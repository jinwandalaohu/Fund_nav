"""持仓加权合成路径（synthetic）。

核心公式（归一化加权）：
    total_weight        = Σ(成功取到行情的持仓权重)
    weighted_change     = Σ(权重 × 个股涨跌幅)
    estimated_change    = weighted_change / total_weight
    estimated_nav       = base_nav × (1 + estimated_change / 100)

为什么必须除以 total_weight：前十大重仓通常只占净值 30%–80%，若直接 Σ(w×r)，
结果会被系统性低估（相当于假设剩余仓位不涨不跌）。归一化等价于"假设未覆盖部分
与重仓股同向同幅"，这是误差的主要来源之一，因此必须把 coverage 一并落库。

执行顺序（evaluate）：
    nav → snapshot → quotes → aggregate → confidence → result
每一步失败的处理策略不同：
    nav 失败        → 直接放弃（没有基准净值无法换算净值）
    snapshot 失败   → 有 fallback_etf 则用代理 ETF，否则抛
    quotes 全失败   → 同上
    coverage == 0   → 同上（拿到了持仓但一只票都没行情）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Sequence

from ..datasource.base import DataSourceError, HoldingsSource, NavSource, QuoteSource
from ..models import (
    Confidence,
    FundConfig,
    Holding,
    HoldingsSnapshot,
    NavPoint,
    Quote,
    ValuationResult,
    ValuationStatus,
)
from ..models import (
    ValuationSource as ValuationSourceTag,
)
from ..utils.http import normalize_code
from ..utils.logger import get_logger
from ..utils.timeutil import iso_timestamp

LOGGER = get_logger(__name__)

# ETF 联接/指数代理的仓位假设：股票仓位通常不是 100%，按 95% 折算更贴近实际净值波动。
# 依据：联接基金合同要求 ≥90% 资产投资目标 ETF，实际多在 95% 左右，
# 剩余 5% 是现金与应收款，不跟随涨跌，直接用 ETF 涨跌会高估。
ETF_POSITION_FACTOR = 0.95

# 置信度分档：达到阈值上限为 high；达到阈值 75% 为 medium；其余 low。
MEDIUM_RATIO = 0.75


@dataclass(frozen=True, slots=True)
class Aggregation:
    """加权结果。coverage 为实际参与计算的权重合计（小数形式）。

    used / missing 保留下来是为了可解释性：日志能说清"哪几只票没取到行情"，
    排查时不需要重跑一遍。
    """

    change_pct: float
    coverage: float
    used: tuple[tuple[Holding, Quote], ...] = ()
    missing: tuple[Holding, ...] = ()

    @property
    def usable_count(self) -> int:
        """参与计算的持仓只数，用一只都没有时置信度直接判定为 low。"""
        return len(self.used)


@dataclass(slots=True)
class SyntheticOutcome:
    """估值结果 + 本次使用的持仓快照（供引擎落库审计）。

    快照随结果一起返回，是为了让引擎在同一次落库中把
    "用了哪份持仓"与"算出什么结果"绑定起来。
    """

    result: ValuationResult
    snapshot: HoldingsSnapshot | None = None


def aggregate_holdings(holdings: Iterable[Holding], quotes: dict[str, Quote]) -> Aggregation:
    """纯函数：加权 + 归一化。无副作用，便于单元测试覆盖边界。

    单只股票缺行情时从分母中剔除（而不是按 0% 涨跌计入），否则会把"没数据"
    错误地当成"没波动"。

    参数 quotes 的键必须是**持仓的原始代码**（与 Holding.asset_code 一致），
    行情键归一化与别名的映射由调用方负责。
    """
    used: list[tuple[Holding, Quote]] = []
    missing: list[Holding] = []
    total_weight = 0.0
    weighted_change = 0.0

    for holding in holdings:
        if holding.weight <= 0:
            # 权重缺失/为 0 的持仓不参与计算，也不需要报告为"缺失行情"。
            continue
        quote = quotes.get(holding.asset_code)
        if quote is None or not quote.is_valid:
            missing.append(holding)
            continue
        total_weight += holding.weight
        weighted_change += holding.weight * quote.change_pct
        used.append((holding, quote))

    if total_weight <= 0:
        # 一只票都没行情：返回 coverage=0，由调用方决定 fallback 还是失败。
        return Aggregation(change_pct=0.0, coverage=0.0, used=tuple(used), missing=tuple(missing))

    return Aggregation(
        # 归一化：除以实际覆盖率，把"未覆盖部分"按同样涨跌幅折算进来。
        change_pct=weighted_change / total_weight,
        coverage=total_weight,
        used=tuple(used),
        missing=tuple(missing),
    )


def classify_confidence(
    coverage: float,
    max_holdings_pct: float,
    usable_count: int,
) -> Confidence:
    """按覆盖率判定置信度。

    纯函数，阈值语义：
    - 一只股票都没取到行情 → low（无论阈值如何）
    - coverage >= max_holdings_pct            → high
    - coverage >= max_holdings_pct × 0.75     → medium
    - 其余                                     → low

    注意 max_holdings_pct 是"基线"而不是"上限"：它不参与估值计算，
    只决定标签，因此同一只基金改这个值不会改变 estimated_nav。
    """
    if usable_count <= 0 or coverage <= 0:
        return Confidence.LOW
    if max_holdings_pct <= 0:
        # 配置层已禁止 <=0，这里防御性返回，避免除零。
        return Confidence.LOW
    ratio = coverage / max_holdings_pct
    if ratio >= 1.0:
        return Confidence.HIGH
    # 浮点误差会让 0.60/0.80 = 0.7499999 跌破 0.75 阈值，故留 1e-9 容差。
    if ratio >= MEDIUM_RATIO - 1e-9:
        return Confidence.MEDIUM
    return Confidence.LOW


class SyntheticValuator:
    """持仓加权合成器：负责取持仓、取行情、加权、判定置信度。

    依赖注入形式（sources + snapshot_loader），因此不依赖数据库与配置加载，
    单测可传入假实现。
    """

    name = "synthetic"

    def __init__(
        self,
        holdings_sources: Sequence[HoldingsSource],
        quote_sources: Sequence[QuoteSource],
        nav_source: NavSource,
        *,
        snapshot_loader: Callable[[str, float], HoldingsSnapshot | None] | None = None,
        snapshot_max_age_hours: float = 24.0,
        etf_position_factor: float = ETF_POSITION_FACTOR,
    ) -> None:
        """snapshot_loader 为 None 时不做快照复用，每次都抓网络（便于单测）。"""
        if not holdings_sources or not quote_sources:
            # 缺少任一源都无法合成，启动即失败，避免运行期才暴露。
            raise ValueError("合成路径至少需要一个持仓源与一个行情源")
        self._holdings_sources = list(holdings_sources)
        self._quote_sources = list(quote_sources)
        self._nav_source = nav_source
        # 快照读取器由引擎注入 db.latest_holdings；为 None 时每次都抓网络。
        self._snapshot_loader = snapshot_loader
        self._snapshot_max_age_hours = snapshot_max_age_hours
        self._etf_factor = etf_position_factor

    def evaluate(self, fund: FundConfig, moment: datetime | None = None) -> SyntheticOutcome:
        """对单只基金做合成估值。失败抛 DataSourceError，由引擎转成 failed 记录。"""
        now = moment or datetime.now()
        ts = iso_timestamp(now)
        # 基准净值是硬依赖，取不到就没有起算点，直接失败（不尝试 fallback：
        # fallback 也需要 nav 才能算净值）。
        nav = self._fetch_nav(fund.code)

        try:
            snapshot = self._load_holdings(fund.code)
            quotes, quote_tag = self._fetch_quotes([h.asset_code for h in snapshot.holdings])
        except DataSourceError as exc:
            # 持仓或行情整体不可用：有代理 ETF 就降级，否则把原始原因抛给引擎。
            if fund.fallback_etf:
                return self._etf_fallback(fund, nav, ts, reason=exc.message)
            raise

        aggregation = aggregate_holdings(snapshot.holdings, quotes)
        if aggregation.missing:
            # 缺行情不是错误，但要留痕：它直接决定 coverage 与 confidence。
            LOGGER.info(
                "%s 有 %d 只重仓股无有效行情，已从覆盖率中剔除: %s",
                fund.code,
                len(aggregation.missing),
                ",".join(h.asset_code for h in aggregation.missing),
            )

        if aggregation.coverage <= 0:
            # 持仓有了、行情一条都没有：与"持仓失败"同等处理。
            reason = "所有重仓股行情均不可用"
            if fund.fallback_etf:
                return self._etf_fallback(fund, nav, ts, reason=reason)
            raise DataSourceError(self.name, f"{fund.code} {reason}")

        confidence = classify_confidence(
            aggregation.coverage, fund.max_holdings_pct, aggregation.usable_count
        )
        # 净值换算：涨跌幅是百分数，故除以 100。
        estimated_nav = nav.nav * (1 + aggregation.change_pct / 100.0)

        result = ValuationResult(
            fund_code=fund.code,
            ts=ts,
            estimated_nav=round(estimated_nav, 4),
            estimated_change_pct=round(aggregation.change_pct, 4),
            base_nav=round(nav.nav, 4),
            coverage_pct=round(aggregation.coverage, 4),
            confidence=confidence,
            source=ValuationSourceTag.SYNTHETIC,
            status=ValuationStatus.AVAILABLE,
            # 形如 eastmoney:mobile+tencent，表明"持仓来自哪、行情来自哪"。
            data_source=f"{snapshot.source}+{quote_tag}",
        )
        return SyntheticOutcome(result=result, snapshot=snapshot)

    # ------------------------------------------------------------------ 内部
    def _fetch_nav(self, fund_code: str) -> NavPoint:
        """取基准净值。失败时包装成带上下文的消息，便于日志定位。"""
        try:
            return self._nav_source.fetch_nav(fund_code)
        except DataSourceError as exc:
            # 没有基准净值就无法把涨跌幅换算成净值，只能放弃本次估值。
            raise DataSourceError(self.name, f"{fund_code} 基准净值缺失: {exc.message}") from exc

    def _load_holdings(self, fund_code: str) -> HoldingsSnapshot:
        """取持仓：优先复用库内快照，过期或不存在时按源链抓取。

        复用快照的意义：季报每季度才更新，盘中每分钟抓一次页面纯属浪费，
        还可能触发源站限流。
        """
        if self._snapshot_loader is not None:
            cached = self._snapshot_loader(fund_code, self._snapshot_max_age_hours)
            if cached and cached.holdings:
                LOGGER.debug("%s 复用 %.0fh 内的持仓快照", fund_code, self._snapshot_max_age_hours)
                return cached

        # 源链顺序由 registry 决定（配置的 holdings_source 优先），逐个尝试。
        errors: list[str] = []
        for source in self._holdings_sources:
            try:
                snapshot = source.fetch_holdings(fund_code)
            except DataSourceError as exc:
                # 已知失败：记录原因后换下一个源。
                errors.append(exc.message)
                LOGGER.debug("持仓源 %s 失败 %s: %s", source.name, fund_code, exc.message)
                continue
            except Exception as exc:  # 解析代码缺陷不应中断整批
                # 未预期异常同样换源，但要打印堆栈，因为它更可能是代码缺陷。
                errors.append(f"{source.name} 未预期异常: {exc}")
                LOGGER.exception("持仓源 %s 未预期异常 %s", source.name, fund_code)
                continue
            if snapshot.holdings:
                return snapshot
            # 源返回了空快照（未抛异常的实现），也视为失败继续换源。
            errors.append(f"{source.name} 返回空持仓")
        raise DataSourceError(self.name, f"{fund_code} 持仓获取失败: " + " | ".join(errors))

    def _fetch_quotes(self, asset_codes: Sequence[str]) -> tuple[dict[str, Quote], str]:
        """返回以【持仓原始代码】为键的报价表。

        行情源统一用带市场前缀的代码（sh600519/hk00700）作为键，而持仓里存的是
        原始代码（600519/00700）。这里做一层归一化别名映射，否则查表会全部落空，
        表现为"所有重仓股无行情"这种误导性错误。
        """
        # alias: 归一化代码 → 持仓原始代码。setdefault 保证同一只票只映射一次。
        alias: dict[str, str] = {}
        for raw in asset_codes:
            if not raw:
                continue
            try:
                normalized = normalize_code(raw)
            except ValueError as exc:
                # 无法归一化说明该持仓代码不规范（如债券代码），跳过但不中断整只基金。
                LOGGER.warning("%s 代码无法归一化，跳过行情: %s", raw, exc)
                continue
            alias.setdefault(normalized, raw)

        pending = list(alias)
        quotes: dict[str, Quote] = {}
        used: list[str] = []

        # 逐源补取：主源没覆盖到的代码才问备用源。
        for source in self._quote_sources:
            if not pending:
                break
            try:
                fetched = source.fetch_quotes(pending)
            except DataSourceError as exc:
                LOGGER.debug("行情源 %s 失败: %s", source.name, exc.message)
                continue
            if not fetched:
                continue
            quotes.update(fetched)
            used.append(source.name)
            pending = [code for code in pending if code not in quotes]

        if not quotes:
            # 全部源都没数据：抛出去让 evaluate 决定 fallback。
            raise DataSourceError(self.name, "所有行情源均未返回有效报价")

        # 反向映射：把归一化键换回持仓里的原始代码，供 aggregate_holdings 查表。
        by_original = {alias[code]: quote for code, quote in quotes.items() if code in alias}
        return by_original, "+".join(used)

    def _etf_fallback(
        self,
        fund: FundConfig,
        nav: NavPoint,
        ts: str,
        *,
        reason: str,
    ) -> SyntheticOutcome:
        """指数/联接基金无持仓可用时，用代理 ETF 的实时涨跌近似。

        该方法产出的 confidence 固定为 low：既没有持仓穿透，又隐含"仓位=95%"假设，
        只能作为方向性参考，source 标 fallback 以便下游区别对待。
        """
        etf_code = fund.fallback_etf
        if not etf_code:
            raise DataSourceError(self.name, f"{fund.code} 无 fallback_etf 可代理")

        quotes, quote_tag = self._fetch_quotes([etf_code])
        # 取任意一条有效报价（正常只有一条，用 next 兼容多返回的情况）。
        quote = next((q for q in quotes.values() if q.is_valid), None)
        if quote is None:
            raise DataSourceError(
                self.name, f"{fund.code} 代理 ETF {etf_code} 无有效行情（原始原因: {reason}）"
            )

        # 仓位折算：ETF 涨 1% 时联接基金净值约涨 0.95%。
        change_pct = quote.change_pct * self._etf_factor
        result = ValuationResult(
            fund_code=fund.code,
            ts=ts,
            estimated_nav=round(nav.nav * (1 + change_pct / 100.0), 4),
            estimated_change_pct=round(change_pct, 4),
            base_nav=round(nav.nav, 4),
            # 没有持仓穿透，固定 low，不参与阈值判定。
            confidence=Confidence.LOW,
            source=ValuationSourceTag.FALLBACK,
            status=ValuationStatus.AVAILABLE,
            data_source=f"fallback:{etf_code}+{quote_tag}",
            # 把降级原因写进 error_msg：这不是失败，但必须可追溯。
            error_msg=f"使用代理ETF估算（{reason}）",
        )
        return SyntheticOutcome(result=result, snapshot=None)
