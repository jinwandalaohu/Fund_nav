"""轮询调度：交易时段判断 + 定时循环 + 异常隔离。

职责很窄：决定"什么时候跑引擎"，不关心跑什么。
估值循环中的任何异常都在本层被吞掉并记录，保证常驻进程不会因单轮失败退出。
"""

from __future__ import annotations

import time
from datetime import datetime

from .config import AppConfig
from .utils.calendar import TradingCalendar
from .utils.logger import get_logger
from .valuation.engine import ValuationEngine

LOGGER = get_logger(__name__)

# 长睡眠切成 300 秒片段，保证 Ctrl+C 响应与配置变更重启的及时性。
_MAX_SLEEP_SLICE = 300.0


class Scheduler:
    """定时轮询器。持有引擎与日历，串行触发估值。"""

    def __init__(
        self,
        engine: ValuationEngine,
        config: AppConfig,
        calendar: TradingCalendar | None = None,
    ) -> None:
        """引擎与配置必填，日历可选（默认按配置的 holidays_file 构造）。"""
        self._engine = engine
        self._config = config
        # 允许注入日历，便于测试用固定休市日构造场景。
        self._calendar = calendar or TradingCalendar(config.runtime.holidays_file)
        self._running = False

    @property
    def calendar(self) -> TradingCalendar:
        """暴露日历供外部（如状态接口）查询交易日与时段。"""
        return self._calendar

    def run_once(self) -> None:
        """执行一次估值。供手动触发（--once）使用，不受交易时段限制。"""
        self._engine.run_once()

    def run_forever(self, max_iterations: int | None = None) -> None:
        """常驻循环。max_iterations 仅用于调试/测试，生产传 None 表示不限次数。"""
        self._running = True
        iteration = 0
        interval = self._config.runtime.interval_seconds
        trade_only = self._config.runtime.trade_time_only

        LOGGER.info(
            "调度启动: 间隔 %ds | 仅交易时段 %s | 共 %d 只基金",
            interval,
            "是" if trade_only else "否",
            len(self._config.funds),
        )

        while self._running:
            now = datetime.now()
            if trade_only and not self._calendar.is_trade_time(now):
                # 非交易时段：睡到下一个开盘时刻，但最多睡 1 小时 ——
                # 这样即使日历判断有误（如临时休市调整），也能及时发现并纠正。
                wait = min(self._calendar.seconds_until_next_window(now), 3600.0)
                LOGGER.info("当前非交易时段，%.0f 秒后重新检查（下次窗口 %s）",
                            wait, self._calendar.next_window_start(now).isoformat(timespec="minutes"))
                self._sleep(wait)
                continue

            started = time.monotonic()
            try:
                self._engine.run_once()
            except Exception as exc:  # 循环必须活下去，单次失败仅记录
                # 引擎内部已隔离单只基金异常，能冒到这里说明是更上层的问题（如磁盘满）。
                LOGGER.exception("本轮估值异常: %s", exc)

            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                LOGGER.info("已达最大轮询次数 %d，退出", max_iterations)
                break

            # 用"间隔 - 本轮耗时"来睡，避免每轮实际周期被计算时间拉长。
            elapsed = time.monotonic() - started
            self._sleep(max(0.0, interval - elapsed))

    def stop(self) -> None:
        """请求停止循环（可从信号处理函数或测试中调用）。"""
        self._running = False

    @staticmethod
    def _sleep(seconds: float) -> None:
        """分段睡眠。time.sleep 一次性睡几小时会让 Ctrl+C 迟迟不响应。"""
        remaining = seconds
        while remaining > 0:
            chunk = min(remaining, _MAX_SLEEP_SLICE)
            time.sleep(chunk)
            remaining -= chunk
