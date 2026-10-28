"""领域模型：贯穿数据源、估值、持久化三层的纯数据结构。

设计原则：这些对象不依赖网络、数据库或配置文件，便于在单元测试中直接构造。
整个系统的数据流转可以概括为四个数据结构：

    DirectQuote   ← 直接估值接口的原始返回     （第三方给的 GSZ/GSZZL）
    HoldingsSnapshot + Quote + NavPoint
                  ↓ 加权合成
    ValuationResult → 一条 SQLite valuation 记录（同时是唯一对外输出）

模块内的枚举都继承 StrEnum，因此 `FundMode.AUTO == "auto"` 成立，
写库/读 YAML 时无需再做类型转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FundMode(StrEnum):
    """估值模式。auto 表示"先取直接估值，失败再降级合成"。

    取值由 YAML 的 funds[].mode 指定，非法值会在 config 层直接报错，
    因此下游 match 语句无需处理"未知模式"（仍保留 default 分支以防新增枚举遗漏）。
    """

    AUTO = "auto"
    DIRECT = "direct"
    SYNTHETIC = "synthetic"


class HoldingsSource(StrEnum):
    """持仓主源偏好。备份源固定为另一个，见 datasource/registry.py。"""

    EASTMONEY = "eastmoney"
    TIANTIAN = "tiantian"


class ValuationStatus(StrEnum):
    """结果状态，决定下游是否可用、是否告警。

    AVAILABLE     成功算出估值
    NOT_SUPPORTED 接口明确没有实时估值（主动基金常态），不是错误，不告警
    FAILED        所有数据源均不可用，需要告警
    """

    AVAILABLE = "available"
    NOT_SUPPORTED = "not_supported"
    FAILED = "failed"


class ValuationSource(StrEnum):
    """估值来源，表示"这个数值是怎么来的"，是下游判断可信度的第一依据。

    API       第三方直接给的盘中估值，权威
    SYNTHETIC 按重仓股加权自算，方向性近似
    FALLBACK  用代理 ETF 近似，误差最大
    """

    API = "api"
    SYNTHETIC = "synthetic"
    FALLBACK = "fallback"


class Confidence(StrEnum):
    """置信度，仅对 synthetic 路径由覆盖率计算；api 固定 high、fallback 固定 low。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FundType(StrEnum):
    """基金粗分类，仅用于 funds.fund_type 展示，不参与任何计算分支。"""

    EQUITY = "equity"
    INDEX = "index"
    LOF = "lof"
    ETF = "etf"
    HYBRID = "hybrid"
    BOND = "bond"
    UNKNOWN = "unknown"


class AssetType(StrEnum):
    """持仓标的类型。当前恒为 STOCK，为将来支持 ETF/指数成分预留。"""

    STOCK = "stock"
    INDEX = "index"
    ETF = "etf"


class WeightUnit(StrEnum):
    """持仓权重的计量口径，由数据源**显式声明**，禁止按数值大小猜测。

    PERCENT  "9.53" / "9.53%" 表示 9.53%（东财 JZBL、F10"占净值比例"列）
    DECIMAL  0.0953 表示 9.53%（少数接口直接给小数）

    为什么必须显式声明：单位是接口字段的固有属性，不是数值的性质。联接基金的
    直接持股只有残余仓位（"0.52" 表示 0.52%），全表数值都小于 1；若用"大于 1
    才除以 100"的启发式推断，会把 0.52% 放大成 52%，coverage 直接冲上 253%、
    置信度误判为 high —— 而结果看起来"有数"，属于最难发现的静默错误。
    """

    PERCENT = "percent"
    DECIMAL = "decimal"


@dataclass(slots=True, frozen=True)
class FundConfig:
    """单只待估值基金的配置（来自 YAML，加载时已完成校验）。

    frozen=True 是刻意的：估值过程中任何环节都不应篡改用户配置，
    需要变更（如 --force 改 trade_time_only）时用 dataclasses.replace 产出新对象。
    """

    code: str
    name: str | None = None
    mode: FundMode = FundMode.AUTO
    holdings_source: HoldingsSource = HoldingsSource.EASTMONEY
    # 置信度分档的基线，不是上限：coverage >= 该值才算 high。
    max_holdings_pct: float = 0.80
    # 无持仓可用时用于代理的场内 ETF（必须 6 位场内代码，不能填场外基金代码）。
    fallback_etf: str | None = None

    @property
    def display_name(self) -> str:
        """日志用名称，缺 name 时退化为代码，避免输出 "None"。"""
        return self.name or self.code


@dataclass(slots=True, frozen=True)
class Holding:
    """一条持仓记录。weight 使用小数形式（0.085 表示占净值 8.5%）。"""

    asset_code: str
    asset_name: str = ""
    weight: float = 0.0
    asset_type: AssetType = AssetType.STOCK


@dataclass(slots=True, frozen=True)
class HoldingsSnapshot:
    """某一时点抓取到的持仓快照，用于审计与 24 小时复用。

    holdings 用 tuple 而非 list，配合 frozen 保证"快照一旦生成就不再变化"——
    缓存复用（db.latest_holdings）时会直接把它交给估值器，若可变则存在被就地修改的风险。
    """

    fund_code: str
    holdings: tuple[Holding, ...]
    snapshot_time: str
    report_date: str | None = None
    source: str = ""

    @property
    def total_weight(self) -> float:
        """前十大合计权重。注意这是"原始合计"，不等于实际参与计算的 coverage。"""
        return sum(h.weight for h in self.holdings)


@dataclass(slots=True, frozen=True)
class Quote:
    """个股/ETF 实时行情。涨跌幅由最新价与昨收价现算，避免依赖数据源字段口径。

    之所以不直接用接口给的涨跌幅字段：不同源对"涨跌幅"的算法基准不一致
    （有的是相对昨收，有的是相对开盘），只有 price/prev_close 两个原始价格是可靠的。
    """

    asset_code: str
    price: float
    prev_close: float
    name: str = ""
    # 接口自带的涨跌幅，仅作排查参考，不参与加权计算。
    change_pct_hint: float | None = None
    ts: str | None = None

    @property
    def is_valid(self) -> bool:
        """价格与昨收都为正才算有效。停牌/未开盘时常返回 0，这类数据必须丢弃。"""
        return self.price > 0 and self.prev_close > 0

    @property
    def change_pct(self) -> float:
        """涨跌幅（百分数）。无效报价返回 0.0，调用方应先看 is_valid。"""
        if not self.is_valid:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100.0


@dataclass(slots=True, frozen=True)
class DirectQuote:
    """直接估值接口的原始返回（天天基金 GSZ/GSZZL 或新浪估值曲线末点）。

    gsz / gszzl 全为 None 表示"该基金没有盘中估值"，这是主动权益基金的正常状态
    （监管要求下线），由 is_empty 表达，不应当作异常。
    """

    fund_code: str
    fund_name: str = ""
    gsz: float | None = None
    gszzl: float | None = None
    gz_time: str | None = None
    nav: float | None = None
    nav_date: str | None = None
    source: str = ""

    @property
    def is_empty(self) -> bool:
        """两个估值字段都缺失才算空；只有其中一个也能算出另一半（见 direct.py）。"""
        return self.gsz is None and self.gszzl is None


@dataclass(slots=True, frozen=True)
class NavPoint:
    """基准净值：nav 为单位净值，date 为净值日期（YYYY-MM-DD）。

    date 目前不落库，仅用于日志与排查；长假后可用它判断基准净值是几个交易日前。
    """

    nav: float
    date: str | None = None


@dataclass(slots=True, frozen=True)
class ValuationResult:
    """估值结果，字段与 SQLite valuation 表一一对应。"""

    fund_code: str
    ts: str
    estimated_nav: float | None = None
    estimated_change_pct: float | None = None
    base_nav: float | None = None
    coverage_pct: float | None = None
    confidence: Confidence | None = None
    source: ValuationSource | None = None
    status: ValuationStatus = ValuationStatus.AVAILABLE
    data_source: str = ""
    error_msg: str | None = None
    # 仅用于 funds 表 upsert，不写入 valuation 表
    fund_name: str | None = None

    def as_row(self) -> tuple:
        """转换为 sqlite 写入用的有序元组，顺序与 db.py 的 INSERT 语句一致。

        枚举在这里统一转成 .value，使 db 层只处理原生类型（SQLite 不认识 StrEnum）。
        """
        return (
            self.fund_code,
            self.ts,
            self.estimated_nav,
            self.estimated_change_pct,
            self.base_nav,
            self.coverage_pct,
            self.confidence.value if self.confidence else None,
            self.source.value if self.source else None,
            self.status.value,
            self.data_source or None,
            self.error_msg,
        )


@dataclass(slots=True)
class RunSummary:
    """单次轮询的汇总指标，用于日志与健康度观察。"""

    total: int = 0
    available: int = 0
    not_supported: int = 0
    failed: int = 0
    elapsed_seconds: float = 0.0
    by_source: dict[str, int] = field(default_factory=dict)
    by_confidence: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_results(cls, results: list[ValuationResult], elapsed: float = 0.0) -> RunSummary:
        """从结果列表汇总。用 match 而非 if/elif，新增状态时漏分支会被静态检查发现。"""
        summary = cls(total=len(results), elapsed_seconds=round(elapsed, 2))
        for r in results:
            match r.status:
                case ValuationStatus.AVAILABLE:
                    summary.available += 1
                case ValuationStatus.NOT_SUPPORTED:
                    summary.not_supported += 1
                case ValuationStatus.FAILED:
                    summary.failed += 1
            if r.source:
                summary.by_source[r.source.value] = summary.by_source.get(r.source.value, 0) + 1
            if r.confidence:
                summary.by_confidence[r.confidence.value] = (
                    summary.by_confidence.get(r.confidence.value, 0) + 1
                )
        return summary
