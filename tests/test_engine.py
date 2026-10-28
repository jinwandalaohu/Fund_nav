"""调度引擎测试：双轨选择与降级策略（用桩替代真实估值器）。

引擎的测试重点是"分派逻辑"，不是计算本身，因此这里用 StubDirect / StubSynthetic
替换两个估值器，通过记录 calls 来断言"谁被调用了、调用了几次"。
数据库用 :memory:，可以直接断言落库结果。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from fund_valuation.config import AppConfig, RuntimeConfig
from fund_valuation.datasource.base import DataSourceError
from fund_valuation.db import Database
from fund_valuation.models import (
    FundConfig,
    FundMode,
    ValuationResult,
    ValuationSource,
    ValuationStatus,
)
from fund_valuation.valuation.engine import ValuationEngine
from fund_valuation.valuation.synthetic import SyntheticOutcome

NOW = datetime(2026, 10, 27, 10, 30, 0)


class StubDirect:
    """假直接估值器：按预设结果表返回，并记录被请求的代码批次。"""

    def __init__(self, results: dict[str, ValuationResult]) -> None:
        """results 是"代码 → 结果"的查表，查不到的代码不出现在返回中。"""
        self.results = results
        self.calls: list[list[str]] = []

    def evaluate(self, codes, moment=None):
        """记录本批请求的代码，模拟直接估值器的批量接口。"""
        self.calls.append(list(codes))
        return {code: self.results[code] for code in codes if code in self.results}


class StubSynthetic:
    """假合成估值器：可返回成功结果或抛 DataSourceError，用于验证降级与失败。"""

    def __init__(self, outcome: SyntheticOutcome | None = None, error: str | None = None) -> None:
        """error 非空时每次调用都抛异常；否则返回给定的成功结果。"""
        self.outcome = outcome
        self.error = error
        self.calls: list[str] = []

    def evaluate(self, fund, moment=None):
        """记录被调用的基金代码，用于断言"哪些模式不该走到合成"。"""
        self.calls.append(fund.code)
        if self.error:
            raise DataSourceError("synthetic", self.error)
        assert self.outcome is not None
        return self.outcome


def _config(*funds: FundConfig) -> AppConfig:
    """构造最小可用配置，数据库固定为内存库。"""
    return AppConfig(funds=tuple(funds), runtime=RuntimeConfig(db_path=":memory:"))


def _result(code: str, status: ValuationStatus, source: ValuationSource | None = None) -> ValuationResult:
    """构造只带状态与来源的直接估值结果（引擎只关心这两项）。"""
    return ValuationResult(fund_code=code, ts=NOW.isoformat(), status=status, source=source)


def _synthetic_ok(code: str) -> SyntheticOutcome:
    """构造一条成功的合成结果，数值随意，只用于断言落库内容。"""
    return SyntheticOutcome(
        result=ValuationResult(
            fund_code=code,
            ts=NOW.isoformat(),
            estimated_change_pct=1.2,
            estimated_nav=2.024,
            base_nav=2.0,
            coverage_pct=0.62,
            confidence=None,
            source=ValuationSource.SYNTHETIC,
            status=ValuationStatus.AVAILABLE,
        )
    )


@pytest.fixture
def db():
    """内存库，测试结束自动关闭。"""
    database = Database(":memory:")
    yield database
    database.close()


def test_auto_downgrades_to_synthetic_when_not_supported(db):
    """auto 模式下接口无实时估值（not_supported）→ 自动降级到合成路径。"""
    fund = FundConfig(code="005827", mode=FundMode.AUTO)
    direct = StubDirect({"005827": _result("005827", ValuationStatus.NOT_SUPPORTED, ValuationSource.API)})
    synthetic = StubSynthetic(_synthetic_ok("005827"))

    summary = ValuationEngine(_config(fund), db, direct, synthetic).run_once(NOW)

    assert summary.available == 1
    assert synthetic.calls == ["005827"]
    row = db.recent_valuations(limit=1)[0]
    assert row["source"] == "synthetic"
    assert row["status"] == "available"


def test_auto_keeps_api_result_without_synthetic(db):
    """auto 模式下直接估值可用 → 直接采用，绝不触发合成（省一次网络开销）。"""
    fund = FundConfig(code="510300", mode=FundMode.AUTO)
    direct = StubDirect({"510300": _result("510300", ValuationStatus.AVAILABLE, ValuationSource.API)})
    synthetic = StubSynthetic(_synthetic_ok("510300"))

    ValuationEngine(_config(fund), db, direct, synthetic).run_once(NOW)

    assert synthetic.calls == []
    assert db.recent_valuations(limit=1)[0]["source"] == "api"


def test_auto_marks_failed_when_both_paths_fail(db):
    """两条路径都失败 → failed，且 error_msg 需同时包含两段原因，便于排查。"""
    fund = FundConfig(code="005827", mode=FundMode.AUTO)
    direct = StubDirect({"005827": _result("005827", ValuationStatus.NOT_SUPPORTED, ValuationSource.API)})
    synthetic = StubSynthetic(error="持仓获取失败")

    summary = ValuationEngine(_config(fund), db, direct, synthetic).run_once(NOW)

    assert summary.failed == 1
    row = db.recent_valuations(limit=1)[0]
    assert row["status"] == "failed"
    assert "持仓获取失败" in row["error_msg"]
    assert "direct" in row["error_msg"]


def test_direct_mode_never_calls_synthetic(db):
    """direct 模式：只用接口值，即使接口没数据也不降级。"""
    fund = FundConfig(code="510300", mode=FundMode.DIRECT)
    direct = StubDirect({"510300": _result("510300", ValuationStatus.AVAILABLE, ValuationSource.API)})
    synthetic = StubSynthetic(_synthetic_ok("510300"))

    ValuationEngine(_config(fund), db, direct, synthetic).run_once(NOW)

    assert synthetic.calls == []
    # direct 模式下不应请求合成路径所需的任何数据
    assert direct.calls == [["510300"]]


def test_synthetic_mode_skips_direct_batch(db):
    """synthetic 模式：连批量估值请求都不该发（不进入 codes 列表）。"""
    fund = FundConfig(code="002611", mode=FundMode.SYNTHETIC)
    direct = StubDirect({})
    synthetic = StubSynthetic(_synthetic_ok("002611"))

    ValuationEngine(_config(fund), db, direct, synthetic).run_once(NOW)

    assert direct.calls == []  # 完全不发直接估值请求，省一次网络往返
    assert synthetic.calls == ["002611"]


def test_summary_counts_by_source_and_confidence(db):
    """汇总应按 source 分组计数，api 与 synthetic 各 1 只。"""
    funds = (
        FundConfig(code="005827", mode=FundMode.AUTO),
        FundConfig(code="510300", mode=FundMode.DIRECT),
    )
    direct = StubDirect(
        {
            "005827": _result("005827", ValuationStatus.NOT_SUPPORTED, ValuationSource.API),
            "510300": _result("510300", ValuationStatus.AVAILABLE, ValuationSource.API),
        }
    )

    summary = ValuationEngine(_config(*funds), db, direct, StubSynthetic(_synthetic_ok("005827"))).run_once(NOW)

    assert summary.total == 2
    assert summary.by_source == {"api": 1, "synthetic": 1}
