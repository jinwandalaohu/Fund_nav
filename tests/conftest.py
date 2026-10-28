"""测试公共桩：用假数据源替代网络，保证单测离线且确定性。

四个 Fake 类分别对应 base.py 的四个抽象角色，行为刻意模仿真实实现的关键特征：
- FakeQuoteSource 的键是【归一化代码】，用来验证别名映射确实生效；
- FakeValuationSource 记录每次收到的代码批次，用来断言"逐级补取"没有重复请求；
- boom=True 模拟整源故障，验证降级路径。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from fund_valuation.datasource.base import (
    DataSourceError,
    HoldingsSource,
    NavSource,
    QuoteSource,
    ValuationSource,
)
from fund_valuation.models import (
    AssetType,
    DirectQuote,
    Holding,
    HoldingsSnapshot,
    NavPoint,
    Quote,
)
from fund_valuation.utils.http import HttpClient, normalize_code


class FakeValuationSource(ValuationSource):
    """假直接估值源：按代码表返回预设 DirectQuote，可切换成"整体故障"。"""

    name = "fake_val"

    def __init__(self, quotes: dict[str, DirectQuote], *, boom: bool = False) -> None:
        """boom=True 时每次调用都抛 DataSourceError，用于验证"全部源失败"。"""
        super().__init__(HttpClient())
        self._quotes = quotes
        self._boom = boom
        # 记录每次被请求的代码列表，用于断言补取逻辑（不应重复请求已覆盖的代码）。
        self.calls: list[list[str]] = []

    def fetch_valuation(self, codes):
        """返回本批里自己有数据的条目；未覆盖的代码不出现在结果中。"""
        self.calls.append(list(codes))
        if self._boom:
            raise DataSourceError(self.name, "模拟故障")
        # 只返回自己有数据的代码，模拟真实接口"未覆盖的代码不出现"的行为。
        return {code: self._quotes[code] for code in codes if code in self._quotes}


class FakeHoldingsSource(HoldingsSource):
    """假持仓源：返回固定持仓列表；列表为空时模拟"解析不到持仓"。"""

    name = "fake_holdings"

    def __init__(self, holdings: list[Holding], report_date: str = "2026-06-30") -> None:
        """holdings 传空列表可模拟"解析不到持仓"，用于触发 fallback 分支。"""
        super().__init__(HttpClient())
        self._holdings = holdings
        self._report_date = report_date

    def fetch_holdings(self, fund_code: str) -> HoldingsSnapshot:
        """返回固定持仓；空列表时按契约抛 DataSourceError。"""
        if not self._holdings:
            raise DataSourceError(self.name, f"{fund_code} 无持仓")
        return HoldingsSnapshot(
            fund_code=fund_code,
            holdings=tuple(self._holdings),
            snapshot_time=datetime.now().replace(microsecond=0).isoformat(),
            report_date=self._report_date,
            source=self.name,
        )


class FakeQuoteSource(QuoteSource):
    """模拟真实行情源的行为：返回的字典以【归一化代码】为键。"""

    name = "fake_quote"

    def __init__(self, quotes: dict[str, Quote], *, boom: bool = False) -> None:
        """构造时就把键归一化，确保与真实行情源（腾讯/新浪）的返回形状一致。"""
        super().__init__(HttpClient())
        # 构造时就归一化，与腾讯/新浪的真实返回保持同一形状。
        self._quotes = {normalize_code(code): quote for code, quote in quotes.items()}
        self._boom = boom

    def fetch_quotes(self, codes):
        """入参是归一化代码，返回也只覆盖能查到的那些。"""
        if self._boom:
            raise DataSourceError(self.name, "模拟行情故障")
        return {code: self._quotes[code] for code in codes if code in self._quotes}


class FakeNavSource(NavSource):
    """假净值源：nav 为 None 时模拟取不到基准净值（应导致整只基金失败）。"""

    name = "fake_nav"

    def __init__(self, nav: float | None = 2.0) -> None:
        """nav=None 表示取不到净值，用于验证"无基准净值则整只基金失败"。"""
        super().__init__(HttpClient())
        self._nav = nav

    def fetch_nav(self, fund_code: str) -> NavPoint:
        """返回固定净值；None 时按契约抛 DataSourceError。"""
        if self._nav is None:
            raise DataSourceError(self.name, "模拟净值缺失")
        return NavPoint(nav=self._nav, date="2026-10-23")


@pytest.fixture
def holdings() -> list[Holding]:
    """两只票、权重合计 0.30 —— 刻意远低于 0.80 基线，用来暴露归一化问题。"""
    return [
        Holding("600519", "贵州茅台", 0.10, AssetType.STOCK),
        Holding("000858", "五粮液", 0.20, AssetType.STOCK),
    ]


@pytest.fixture
def quotes() -> dict[str, Quote]:
    """与上面持仓对应的行情：一涨（+10%）一跌（-2%），加权结果为 +2%。"""
    return {
        "600519": Quote("600519", price=110.0, prev_close=100.0, name="贵州茅台"),  # +10%
        "000858": Quote("000858", price=98.0, prev_close=100.0, name="五粮液"),  # -2%
    }
