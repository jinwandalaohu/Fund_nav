"""直接取值路径：消费第三方估值接口的 GSZ / GSZZL。

该路径的数据权威性最高（对 LOF / ETF / 部分指数基金而言就是官方盘中估值），
因此 confidence 固定为 high，coverage_pct 留空（不涉及持仓穿透）。

每个基金最终会落到四种结果之一：
    1. 所有源都没返回该基金            → FAILED
    2. 返回了但 GSZ/GSZZL 都为空        → NOT_SUPPORTED（主动基金常态）
    3. 有值但 GZTIME 不是今天           → NOT_SUPPORTED（隔夜旧值，不能当实时数据）
    4. 有值且当日                    → AVAILABLE（confidence=high）

只有第 4 种会给 estimated_nav 赋值，因此下游必须先看 status 再取数。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from ..config import RuntimeConfig
from ..datasource.base import DataSourceError, ValuationSource
from ..models import (
    Confidence,
    DirectQuote,
    ValuationResult,
    ValuationStatus,
)
from ..models import (
    ValuationSource as ValuationSourceTag,
)
from ..utils.http import chunked
from ..utils.logger import get_logger
from ..utils.timeutil import iso_timestamp, same_day

LOGGER = get_logger(__name__)


class DirectValuator:
    """按优先级串行尝试多个估值源，凑齐尽可能多的基金。

    设计取舍：按"缺失代码"逐级补取，而不是整批重试。主源返回了 48/50 只，
    就只拿剩下 2 只去问备用源，能显著减少无效请求。
    """

    def __init__(
        self,
        sources: Sequence[ValuationSource],
        runtime: RuntimeConfig,
    ) -> None:
        """sources 的顺序即优先级；batch_size 与 validate_gztime 来自运行时配置。"""
        if not sources:
            raise ValueError("直接估值路径至少需要一个估值源")
        self._sources = list(sources)
        # 批量接口单次上限（天天基金实测 50），由配置的 batch_size 控制。
        self._batch_size = runtime.batch_size
        # 是否校验 GZTIME 为当日；关掉后隔夜旧值也会被当作实时数据，仅调试用。
        self._validate_gztime = runtime.validate_gztime

    def evaluate(self, fund_codes: Sequence[str], moment: datetime | None = None) -> dict[str, ValuationResult]:
        """对一批基金做直接估值，返回 {fund_code: ValuationResult}。

        返回的字典一定覆盖入参里的每一个代码（缺数据的会给出 FAILED 结果），
        这样引擎侧无需再处理"结果缺失"的情况。
        """
        if not fund_codes:
            return {}

        now = moment or datetime.now()
        # 同一轮所有基金共用一个 ts：保证同批数据时间戳一致，便于按轮次聚合查询。
        ts = iso_timestamp(now)
        pending = list(dict.fromkeys(fund_codes))  # 去重且保持顺序
        quotes: dict[str, DirectQuote] = {}

        # 逐源补取：每轮只请求上一轮没覆盖到的代码。
        for source in self._sources:
            if not pending:
                break
            fetched = self._fetch(source, pending)
            if not fetched:
                continue
            quotes.update(fetched)
            pending = [code for code in pending if code not in quotes]
            LOGGER.debug("估值源 %s 返回 %d 条，剩余 %d 条待补", source.name, len(fetched), len(pending))

        results: dict[str, ValuationResult] = {}
        # 按入参顺序（而非 quotes 的键顺序）遍历，保证结果与配置顺序一致。
        for code in fund_codes:
            quote = quotes.get(code)
            results[code] = self._to_result(code, quote, ts, now.date())
        return results

    def _fetch(self, source: ValuationSource, codes: Sequence[str]) -> dict[str, DirectQuote]:
        """按 batch_size 分批请求单个源。

        单批失败只告警不抛出：一批 50 只里可能有几只代码已清盘/合并导致整批失败，
        直接抛出会让整个源失效，而实际大部分数据是好的。
        """
        merged: dict[str, DirectQuote] = {}
        for batch in chunked(codes, self._batch_size):
            try:
                merged.update(source.fetch_valuation(batch))
            except DataSourceError as exc:
                LOGGER.warning("估值源 %s 本批失败(%d 只): %s", source.name, len(batch), exc.message)
            except Exception as exc:  # 兜底：数据源实现缺陷不应拖垮整批
                LOGGER.exception("估值源 %s 未预期异常: %s", source.name, exc)
        return merged

    def _to_result(
        self,
        fund_code: str,
        quote: DirectQuote | None,
        ts: str,
        today: date,
    ) -> ValuationResult:
        """把原始报价翻译成估值结果。四个分支的顺序不能调换。

        顺序理由：先判"没拿到数据"（FAILED），再判"拿到了但没估值"（NOT_SUPPORTED），
        最后才判时间。若把时间校验放在最前面，空值会因为 gz_time 也缺失而
        被误判成"隔夜旧值"，错误信息就会误导排查方向。
        """
        if quote is None:
            # 所有源都没有这只基金：可能是代码填错、已清盘，或接口整批失败。
            return ValuationResult(
                fund_code=fund_code,
                ts=ts,
                status=ValuationStatus.FAILED,
                data_source=",".join(s.name for s in self._sources),
                error_msg="所有直接估值源均未返回该基金数据",
            )

        if quote.is_empty:
            # 主动权益基金被监管取消盘中估值，这是合法状态，不是错误。
            return ValuationResult(
                fund_code=fund_code,
                ts=ts,
                base_nav=quote.nav,
                source=ValuationSourceTag.API,
                status=ValuationStatus.NOT_SUPPORTED,
                data_source=quote.source or "tiantian",
                fund_name=quote.fund_name or None,
            )

        if self._validate_gztime and not same_day(quote.gz_time, today):
            # 隔夜旧值一旦被当作实时数据写库，下游会误判当日行情。
            # 注意这里仍写入 estimated_change_pct 便于排查，但 status 是 not_supported，
            # 下游取数前必须看 status。
            return ValuationResult(
                fund_code=fund_code,
                ts=ts,
                base_nav=quote.nav,
                estimated_change_pct=quote.gszzl,
                source=ValuationSourceTag.API,
                status=ValuationStatus.NOT_SUPPORTED,
                data_source=quote.source or "tiantian",
                error_msg=f"估值时间非当日(GZTIME={quote.gz_time})，按无实时估值处理",
                fund_name=quote.fund_name or None,
            )

        change_pct = quote.gszzl
        estimated_nav = quote.gsz
        if estimated_nav is None and change_pct is not None and quote.nav:
            # 部分源（如新浪）只给涨跌幅，用基准净值反推估算净值，保证两列口径一致。
            estimated_nav = quote.nav * (1 + change_pct / 100.0)
        if change_pct is None and estimated_nav is not None and quote.nav:
            # 反向补齐：只给 GSZ 时反算涨跌幅，避免下游拿到一半数据。
            change_pct = (estimated_nav / quote.nav - 1) * 100.0

        return ValuationResult(
            fund_code=fund_code,
            ts=ts,
            estimated_nav=_round(estimated_nav, 4),
            estimated_change_pct=_round(change_pct, 4),
            base_nav=_round(quote.nav, 4),
            # 直接取值即官方口径，置信度不参与阈值判定，固定 high。
            confidence=Confidence.HIGH,
            source=ValuationSourceTag.API,
            status=ValuationStatus.AVAILABLE,
            data_source=quote.source or "tiantian",
            fund_name=quote.fund_name or None,
        )


def _round(value: float | None, digits: int) -> float | None:
    """四舍五入，None 透传。净值保留 4 位是行业惯例（0.0001 元）。"""
    return None if value is None else round(float(value), digits)
