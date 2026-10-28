"""加权归一化计算测试（不含网络）。

覆盖三块纯逻辑：
- aggregate_holdings  加权 + 归一化 + 缺行情剔除
- classify_confidence 置信度三档边界
- SyntheticValuator   端到端（含 fallback 分支）
"""

from __future__ import annotations

import pytest

from fund_valuation.models import AssetType, FundConfig, Holding, Quote, ValuationStatus
from fund_valuation.valuation.synthetic import (
    Aggregation,
    SyntheticValuator,
    aggregate_holdings,
    classify_confidence,
)

from .conftest import FakeHoldingsSource, FakeNavSource, FakeQuoteSource


def _quote(code: str, price: float, prev_close: float) -> Quote:
    """构造行情的简写，只关心价格对。"""
    return Quote(code, price=price, prev_close=prev_close)


def test_normalization_sums_to_weighted_average(holdings, quotes):
    """Σ(w×r) 必须除以 Σw，否则覆盖率不足时会系统性低估。"""
    result = aggregate_holdings(holdings, quotes)

    # (0.10×10% + 0.20×(-2%)) / 0.30 = (1.0 - 0.4)/0.3 = 2.0%
    assert result.change_pct == pytest.approx(2.0)
    assert result.coverage == pytest.approx(0.30)
    assert result.usable_count == 2
    assert result.missing == ()


def test_missing_quote_is_excluded_from_denominator(holdings, quotes):
    """缺行情的股票必须同时从分子与分母剔除，不能按 0% 涨跌计入。"""
    partial = {"600519": quotes["600519"]}
    result = aggregate_holdings(holdings, partial)

    assert result.change_pct == pytest.approx(10.0)  # 只剩茅台，涨 10%
    assert result.coverage == pytest.approx(0.10)
    assert [h.asset_code for h in result.missing] == ["000858"]


def test_invalid_quote_price_treated_as_missing():
    """停牌股返回 price=0，应视为无行情而不是 0% 波动。"""
    holdings = [Holding("600519", "贵州茅台", 0.5, AssetType.STOCK)]
    quotes = {"600519": Quote("600519", price=0.0, prev_close=100.0)}
    result = aggregate_holdings(holdings, quotes)

    # 唯一一只票无行情 → coverage 归零，由上层决定 fallback 还是失败。
    assert result.coverage == 0.0
    assert result.change_pct == 0.0


def test_all_missing_yields_zero_coverage():
    """行情表为空时返回 coverage=0 的空聚合，而不是抛异常。"""
    holdings = [Holding("600519", "贵州茅台", 0.5, AssetType.STOCK)]
    result = aggregate_holdings(holdings, {})

    assert isinstance(result, Aggregation)
    assert result.coverage == 0.0
    assert result.usable_count == 0


def test_zero_weight_holding_ignored():
    """权重为 0 的持仓不参与计算，也不计入覆盖率。"""
    holdings = [Holding("600519", "贵州茅台", 0.0, AssetType.STOCK)]
    quotes = {"600519": _quote("600519", 110.0, 100.0)}

    assert aggregate_holdings(holdings, quotes).coverage == 0.0


def test_confidence_thresholds():
    """置信度三档边界：达到基线为 high，达到 75% 基线为 medium，其余 low。"""
    assert classify_confidence(0.80, 0.80, 10).value == "high"
    assert classify_confidence(0.60, 0.80, 10).value == "medium"  # 0.75 倍阈值
    assert classify_confidence(0.30, 0.80, 10).value == "low"
    # 一只票都没有 → low；基线为 0（非法值）→ low，均不得抛除零异常。
    assert classify_confidence(0.0, 0.80, 0).value == "low"
    assert classify_confidence(0.90, 0.0, 5).value == "low"


def test_synthetic_end_to_end(holdings, quotes):
    """端到端：coverage 0.3 / 涨跌 2% / 基准净值 2.0 → 估算净值 2.04。"""
    valuator = SyntheticValuator(
        holdings_sources=[FakeHoldingsSource(holdings)],
        quote_sources=[FakeQuoteSource(quotes)],
        nav_source=FakeNavSource(2.0),
    )
    # 基线设为 0.30 与实测覆盖率相等，用于验证 high 档。
    fund = FundConfig(code="005827", max_holdings_pct=0.30, mode="auto")

    outcome = valuator.evaluate(fund)
    result = outcome.result

    assert result.status is ValuationStatus.AVAILABLE
    assert result.estimated_change_pct == pytest.approx(2.0)
    assert result.estimated_nav == pytest.approx(2.04)
    assert result.coverage_pct == pytest.approx(0.30)
    assert result.confidence.value == "high"
    assert result.source.value == "synthetic"
    assert outcome.snapshot is not None
    assert "fake_holdings" in result.data_source


def test_synthetic_raises_when_no_quote_at_all(holdings):
    """有持仓但行情全空、且未配置 fallback_etf → 必须抛错（引擎会转成 failed）。"""
    valuator = SyntheticValuator(
        holdings_sources=[FakeHoldingsSource(holdings)],
        quote_sources=[FakeQuoteSource({})],
        nav_source=FakeNavSource(2.0),
    )
    with pytest.raises(Exception) as excinfo:
        valuator.evaluate(FundConfig(code="005827", max_holdings_pct=0.8))
    assert "行情" in str(excinfo.value)


def test_etf_fallback_used_when_holdings_unavailable():
    """无持仓但有代理 ETF 时走 fallback，confidence 必须为 low。"""
    etf_quote = {"sh518880": Quote("sh518880", price=5.05, prev_close=5.00)}  # +1%
    valuator = SyntheticValuator(
        holdings_sources=[FakeHoldingsSource([])],
        quote_sources=[FakeQuoteSource(etf_quote)],
        nav_source=FakeNavSource(1.0),
    )
    fund = FundConfig(code="002611", fallback_etf="sh518880", max_holdings_pct=0.5)

    result = valuator.evaluate(fund).result

    assert result.status is ValuationStatus.AVAILABLE
    assert result.source.value == "fallback"
    assert result.confidence.value == "low"
    assert result.estimated_change_pct == pytest.approx(0.95)  # 1% × 95% 仓位假设
    assert result.estimated_nav == pytest.approx(1.0095)
