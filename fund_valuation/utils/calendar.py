"""交易时段与交易日历判断。

沪深两市连续竞价时段为 09:30–11:30、13:00–15:00。
节假日采用内置表 + 可选外部文件覆盖：内置表只覆盖近两年，法定安排每年由交易所公告，
因此支持用 holidays_file 指向自定义文件，避免硬编码过期后误判。

本模块只做"时间问题"，不关心估值逻辑；调度器据此决定是否跳过本轮、
以及非交易时段应该睡到什么时候。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from .logger import get_logger

LOGGER = get_logger(__name__)

# 连续竞价时段边界（含端点：15:00 整仍需抓一次收盘估值）。
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

# 内置休市日（含法定节假日与调休）。数据仅供参考，
# 请每年核对交易所公告，或用 holidays_file 覆盖。
_BUILTIN_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2025
        "2025-01-01",
        "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",
        "2025-04-04",
        "2025-05-01", "2025-05-02", "2025-05-05",
        "2025-06-02",
        "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
        # 2026
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
        "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
    }
)


class TradingCalendar:
    """可覆盖休市日集合的交易日历。

    休市日集合 = 内置表 ∪ 外部文件，外部文件只能"增加"休市日，不能删除，
    因此调休补班（周末开市）仍需按周末处理 —— 这是 A 股极少见的例外，暂不支持。
    """

    def __init__(self, holidays_file: str | Path | None = None) -> None:
        """初始化即并入内置休市日；给了 holidays_file 就再叠加外部文件。"""
        self._holidays: set[str] = set(_BUILTIN_HOLIDAYS)
        if holidays_file:
            self.extend_from_file(holidays_file)

    def extend_from_file(self, path: str | Path) -> None:
        """从文本文件加载休市日，每行一个 YYYY-MM-DD（# 开头为注释）。

        逐行校验并跳过非法行，而不是整体报错：运营维护的日期文件常有手误，
        因为一行写错就停掉整个估值进程代价太大。
        """
        file_path = Path(path)
        if not file_path.exists():
            LOGGER.warning("休市日文件不存在，忽略: %s", file_path)
            return
        loaded = 0
        for line in file_path.read_text(encoding="utf-8").splitlines():
            # 允许行尾注释，方便标注"国庆"这类说明。
            item = line.split("#", 1)[0].strip()
            if not item:
                continue
            try:
                date.fromisoformat(item)
            except ValueError:
                LOGGER.warning("休市日格式非法，已跳过: %s", item)
                continue
            self._holidays.add(item)
            loaded += 1
        LOGGER.info("已从 %s 加载 %d 个休市日", file_path, loaded)

    def is_trading_day(self, day: date | None = None) -> bool:
        """是否为交易日：非周末且不在休市日集合内。"""
        current = day or datetime.now().date()
        if current.weekday() >= 5:  # 周六、周日
            return False
        return current.isoformat() not in self._holidays

    def is_trade_time(self, moment: datetime | None = None) -> bool:
        """当前时刻是否处于连续竞价时段内。"""
        current = moment or datetime.now()
        if not self.is_trading_day(current.date()):
            return False
        clock = current.time()
        # 两段独立判断，午休 11:30–13:00 自然落在区间外。
        return (MORNING_OPEN <= clock <= MORNING_CLOSE) or (
            AFTERNOON_OPEN <= clock <= AFTERNOON_CLOSE
        )

    def next_window_start(self, moment: datetime | None = None) -> datetime:
        """计算下一次可估值时刻，用于非交易时段的长睡眠。

        逻辑：当天尚未开盘 → 当日 09:30；午休 → 当日 13:00；已收盘/非交易日 → 下一交易日 09:30。
        """
        current = (moment or datetime.now()).replace(microsecond=0)
        day = current.date()
        clock = current.time()

        # 先看"今天还有没有机会"：未开盘或午休，两者都能给出确定的当日时刻。
        if self.is_trading_day(day) and clock < AFTERNOON_CLOSE:
            if clock < MORNING_OPEN:
                return datetime.combine(day, MORNING_OPEN)
            if MORNING_CLOSE < clock < AFTERNOON_OPEN:
                return datetime.combine(day, AFTERNOON_OPEN)

        # 否则从明天开始逐日探测，跳过周末与休市日。
        probe = day + timedelta(days=1)
        for _ in range(30):  # 最长连休不会超过 10 天，30 天足够兜底
            if self.is_trading_day(probe):
                return datetime.combine(probe, MORNING_OPEN)
            probe += timedelta(days=1)
        # 理论上不可达；退化为次日开盘，避免死循环。
        return datetime.combine(day + timedelta(days=1), MORNING_OPEN)

    def seconds_until_next_window(self, moment: datetime | None = None) -> float:
        """距下一个交易窗口的秒数。下限 1 秒，防止 sleep(0) 造成忙等。"""
        current = moment or datetime.now()
        return max(1.0, (self.next_window_start(current) - current).total_seconds())
