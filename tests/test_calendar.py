"""交易日历测试：交易时段边界与下一次窗口计算。

时段判断是调度器的开关，边界（09:30 / 11:30 / 13:00 / 15:00）必须精确，
因此用参数化把每个临界点都覆盖一遍。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from fund_valuation.utils.calendar import TradingCalendar


@pytest.fixture
def calendar() -> TradingCalendar:
    """默认日历（仅内置休市日表）。"""
    return TradingCalendar()


def test_weekend_is_not_trading_day(calendar):
    """周六不算交易日，周一算。"""
    assert calendar.is_trading_day(datetime(2026, 10, 24).date()) is False  # 周六
    assert calendar.is_trading_day(datetime(2026, 10, 26).date()) is True   # 周一


def test_holiday_is_not_trading_day(calendar):
    """法定节假日休市（2026-10-01 国庆）。"""
    assert calendar.is_trading_day(datetime(2026, 10, 1).date()) is False  # 国庆


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-10-27 09:29", False),  # 开盘前 1 分钟
        ("2026-10-27 09:30", True),   # 开盘（含端点）
        ("2026-10-27 11:30", True),   # 上午收盘（含端点）
        ("2026-10-27 12:00", False),  # 午休
        ("2026-10-27 13:00", True),   # 下午开盘
        ("2026-10-27 15:00", True),   # 收盘（含端点，仍需抓收盘估值）
        ("2026-10-27 15:01", False),  # 收盘后
        ("2026-10-24 10:00", False),  # 周六
    ],
)
def test_is_trade_time(calendar, moment, expected):
    """时段边界逐一验证，含端点语义：09:30 与 15:00 都算交易时段内。"""
    assert calendar.is_trade_time(datetime.strptime(moment, "%Y-%m-%d %H:%M")) is expected


def test_next_window_from_midday_break(calendar):
    """午休时下一次窗口应为当日 13:00，而不是明天。"""
    moment = datetime(2026, 10, 27, 12, 0)
    assert calendar.next_window_start(moment) == datetime(2026, 10, 27, 13, 0)


def test_next_window_after_close_rolls_to_next_trading_day(calendar):
    """周五收盘后应跳过周末，落到下周一 09:30。"""
    moment = datetime(2026, 10, 30, 16, 0)  # 周五收盘后，周六周日休市
    assert calendar.next_window_start(moment) == datetime(2026, 11, 2, 9, 30)


def test_custom_holidays_file(tmp_path):
    """外部休市日文件生效：可补充内置表未覆盖的临时休市。"""
    path = tmp_path / "holidays.txt"
    path.write_text("# 临时休市\n2026-10-28\n", encoding="utf-8")

    calendar = TradingCalendar(path)
    assert calendar.is_trading_day(datetime(2026, 10, 28).date()) is False
