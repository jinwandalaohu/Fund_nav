"""数据源抽象层。

上层（估值引擎）只依赖这里的抽象接口，因此新增数据源不需要改动估值逻辑，
单元测试也可以直接注入假的实现。

四种角色对应两条估值路径所需的全部外部数据：
    ValuationSource → direct 路径（基金的盘中估值）
    HoldingsSource  → synthetic 路径（重仓股与权重）
    QuoteSource     → synthetic 路径（个股实时行情）
    NavSource       → synthetic 路径（基准单位净值）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ..models import DirectQuote, Holding, HoldingsSnapshot, NavPoint, Quote
from ..utils.http import HttpClient


class DataSourceError(RuntimeError):
    """数据源统一异常。所有数据源对外只抛这一种异常，便于调度层统一降级。

    契约：数据源内部无论是网络失败、响应格式异常还是解析不出数据，都必须包装成
    本异常抛出（并带上数据源名字），上层只需 except DataSourceError 即可实现降级，
    不需要了解各数据源的内部细节。
    """

    def __init__(self, source: str, message: str) -> None:
        """source 为数据源名（用于定位），message 为不含前缀的原始原因。"""
        super().__init__(f"[{source}] {message}")
        self.source = source
        # 保留未包装的原始消息：调度层拼装多源失败原因时用它，避免日志出现嵌套前缀。
        self.message = message


class BaseDataSource(ABC):
    """所有数据源的公共基类：持有 HTTP 客户端，统一 name 标识。

    约定：构造函数只接收 HttpClient，不接收配置或数据库 —— 保证数据源层可单测、
    可替换，配置差异一律通过构造参数显式传入。
    """

    name: str = "base"

    def __init__(self, http: HttpClient) -> None:
        """注入唯一的 HTTP 客户端。子类不得自行 requests.get，保证超时/UA 策略统一。"""
        self._http = http

    def __repr__(self) -> str:  # 便于日志排查
        """日志里直接打印实例即可看出是哪个源。"""
        return f"<{type(self).__name__} name={self.name}>"


class ValuationSource(BaseDataSource):
    """直接估值源：批量返回 GSZ / GSZZL。"""

    @abstractmethod
    def fetch_valuation(self, codes: Sequence[str]) -> dict[str, DirectQuote]:
        """返回 {fund_code: DirectQuote}。未覆盖的代码不出现在结果中。

        重要约定：基金"没有估值数据"不算错误，必须返回一个 gsz/gszzl 为 None 的
        DirectQuote（令 is_empty 为真）；只有"接口本身不可用"才抛 DataSourceError。
        否则主动基金的正常状态会被误判为失败。
        """
        ...


class HoldingsSource(BaseDataSource):
    """持仓源：返回某只基金的最新前十大重仓股。"""

    @abstractmethod
    def fetch_holdings(self, fund_code: str) -> HoldingsSnapshot:
        """抓取前十大重仓股。解析不到任何持仓时必须抛 DataSourceError。"""
        ...

    @staticmethod
    def _require_holdings(fund_code: str, holdings: list[Holding], source: str) -> list[Holding]:
        """空持仓视为失败。

        为什么把"空结果"升级为异常：持仓源返回空通常是页面改版或接口降级，
        若静默返回空快照，上层会把它当成"这只基金没有股票仓位"，
        进而写出 coverage=0 的错误估值。显式失败才能触发换源或标 failed。
        """
        if not holdings:
            raise DataSourceError(source, f"{fund_code} 未解析到任何持仓")
        return holdings


class QuoteSource(BaseDataSource):
    """行情源：批量返回个股/ETF 实时价与昨收价。"""

    @abstractmethod
    def fetch_quotes(self, codes: Sequence[str]) -> dict[str, Quote]:
        """返回 {normalized_code: Quote}。停牌或无数据的代码不出现在结果中。

        注意键是**归一化后**的代码（sh600519 / hk00700），与持仓里的原始代码不同，
        调用方（synthetic._fetch_quotes）负责做别名映射。
        """
        ...


class NavSource(BaseDataSource):
    """基准净值源：返回最近一个交易日的单位净值。"""

    @abstractmethod
    def fetch_nav(self, fund_code: str) -> NavPoint:
        """取基准净值。取不到必须抛异常 —— 没有基准净值就无法把涨跌幅换算成净值。"""
        ...
