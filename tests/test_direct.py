"""直接取值路径测试：空估值、隔夜旧值、多源补取、净值反推。"""

from __future__ import annotations

from datetime import datetime

import pytest

from fund_valuation.config import RuntimeConfig
from fund_valuation.models import DirectQuote, ValuationStatus
from fund_valuation.valuation.direct import DirectValuator

from .conftest import FakeValuationSource

# 固定"当前时刻"，让 GZTIME 校验可复现（避免测试随真实日期漂移）。
NOW = datetime(2026, 10, 27, 10, 30, 0)


def _valuator(*sources) -> DirectValuator:
    """构造被测对象。batch_size=2 用于在多代码场景下验证分批逻辑。"""
    return DirectValuator(list(sources), RuntimeConfig(batch_size=2, validate_gztime=True))


def test_available_quote_uses_api_values():
    """接口给了完整 GSZ/GSZZL 时，应原样采用并标 api + high。"""
    source = FakeValuationSource(
        {
            "510300": DirectQuote(
                "510300", "沪深300ETF", gsz=4.1234, gszzl=1.25, gz_time="2026-10-27 10:29",
                nav=4.0725, source="tiantian",
            )
        }
    )
    result = _valuator(source).evaluate(["510300"], NOW)["510300"]

    assert result.status is ValuationStatus.AVAILABLE
    assert result.source.value == "api"
    assert result.confidence.value == "high"
    assert result.estimated_nav == pytest.approx(4.1234)
    assert result.estimated_change_pct == pytest.approx(1.25)
    assert result.base_nav == pytest.approx(4.0725)


def test_empty_gsz_is_not_supported_not_failed():
    """主动基金无 GSZ 是合法业务状态，不能记为 failed。"""
    source = FakeValuationSource(
        {"005827": DirectQuote("005827", "易方达蓝筹", nav=2.1, gsz=None, gszzl=None)}
    )
    result = _valuator(source).evaluate(["005827"], NOW)["005827"]

    assert result.status is ValuationStatus.NOT_SUPPORTED
    assert result.source.value == "api"
    assert result.estimated_nav is None
    # 即便没有估值，base_nav 仍要落库，供合成路径或人工核对使用。
    assert result.base_nav == pytest.approx(2.1)


def test_stale_gztime_downgraded():
    """GZTIME 是前一交易日 → 隔夜旧值，必须降级为 not_supported 并说明原因。"""
    source = FakeValuationSource(
        {"510300": DirectQuote("510300", gsz=4.0, gszzl=0.5, gz_time="2026-10-24 15:00", nav=3.98)}
    )
    result = _valuator(source).evaluate(["510300"], NOW)["510300"]

    assert result.status is ValuationStatus.NOT_SUPPORTED
    assert "GZTIME" in (result.error_msg or "")


def test_second_source_fills_missing_codes():
    """主源覆盖 1 只、备用源补另 1 只，且备用源只应收到缺失的那一只。"""
    primary = FakeValuationSource(
        {
            "510300": DirectQuote(
                "510300", gsz=4.0, gszzl=1.0, gz_time="2026-10-27 10:29", source="primary"
            )
        }
    )
    backup = FakeValuationSource(
        {
            "161725": DirectQuote(
                "161725", gsz=1.1, gszzl=-0.5, gz_time="2026-10-27 10:29", source="backup"
            )
        }
    )

    results = _valuator(primary, backup).evaluate(["510300", "161725"], NOW)

    assert results["510300"].data_source == "primary"
    assert results["161725"].status is ValuationStatus.AVAILABLE
    assert results["161725"].data_source == "backup"
    # 主源已覆盖的代码不应重复请求备用源
    assert backup.calls == [["161725"]]


def test_all_sources_failed_marks_failed():
    """所有源都异常 → failed，且 estimated_nav 为 NULL（不能是 0）。"""
    source = FakeValuationSource({}, boom=True)
    result = _valuator(source).evaluate(["999999"], NOW)["999999"]

    assert result.status is ValuationStatus.FAILED
    assert result.estimated_nav is None
    assert result.error_msg


def test_change_pct_derived_from_nav_when_missing():
    """只给 GSZ 不给 GSZZL 时，用基准净值反推涨跌幅，保证两列口径一致。"""
    source = FakeValuationSource(
        {"510300": DirectQuote("510300", gsz=4.2, gszzl=None, gz_time="2026-10-27 10:29", nav=4.0)}
    )
    result = _valuator(source).evaluate(["510300"], NOW)["510300"]

    # (4.2 / 4.0 - 1) × 100 = 5%
    assert result.estimated_change_pct == pytest.approx(5.0)
