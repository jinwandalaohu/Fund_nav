"""时间处理小工具：统一的 ISO8601 输出与宽松的接口时间解析。"""

from __future__ import annotations

from datetime import date, datetime

# 接口时间格式不统一，按"最具体 → 最宽松"排列，逐个尝试。
_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


def iso_timestamp(moment: datetime | None = None) -> str:
    """输出秒级 ISO8601（不带微秒），与 valuation.ts 的存储格式一致。

    必须去微秒：valuation 表有 UNIQUE(fund_code, ts) 约束，若带微秒，
    同一秒内的两次计算会被当成两条记录，且字符串比较排序也会被微秒干扰。
    """
    current = moment or datetime.now()
    return current.replace(microsecond=0).isoformat()


def parse_datetime(value: object) -> datetime | None:
    """解析接口返回的时间字符串，失败返回 None。

    接口时间格式不统一（"2025-06-30 15:00" / "2025-06-30 15:00:00" / "2025-06-30"），
    逐个格式尝试比正则更不容易漏。
    """
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def same_day(value: object, reference: date | None = None) -> bool:
    """判断接口时间是否与参考日期同一天（用于识别隔夜旧估值）。

    解析失败时返回 False —— 宁可把"时间不可信"的估值当作隔夜旧值丢弃，
    也不要用一条无法校验时间的数值去写库。
    """
    parsed = parse_datetime(value)
    if parsed is None:
        return False
    return parsed.date() == (reference or datetime.now().date())
