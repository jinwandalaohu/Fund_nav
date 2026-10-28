"""YAML 配置加载与校验。

校验策略是"宁可启动失败，也不要静默降级"：枚举值写错、字段拼错、阈值超范围
都在启动阶段抛 ConfigError 并指出具体位置，而不是悄悄用默认值跑出一批错数据。

分层：
    load_config  读文件 → YAML 解析 → 结构校验 → 组装 AppConfig
    _build_fund  逐只基金校验字段类型与取值范围
    _build_runtime 逐项校验运行时参数
    各种 _to_* / _parse_enum  提供"带字段名与位置"的统一错误消息
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import FundConfig, FundMode, HoldingsSource
from .utils.http import DEFAULT_USER_AGENT

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# 白名单：任何未知字段都直接报错，用来兜住拼写错误
# （如 max_holding_pct 少个 s 会被静默忽略并退回默认值 0.80，非常难发现）。
_TOP_KEYS = {"funds", "runtime"}
_FUND_KEYS = {"code", "name", "mode", "holdings_source", "max_holdings_pct", "fallback_etf"}
_RUNTIME_KEYS = {
    "interval_seconds",
    "trade_time_only",
    "db_path",
    "request_timeout",
    "batch_size",
    "user_agent",
    "log_level",
    "max_workers",
    "validate_gztime",
    "retry_times",
    "holidays_file",
}


class ConfigError(ValueError):
    """配置非法。消息面向使用者，必须给出可定位的信息。"""


@dataclass(slots=True, frozen=True)
class RuntimeConfig:
    """运行时参数。全部有默认值，YAML 的 runtime 段可以整体省略。"""

    # 盘中轮询间隔（秒）
    interval_seconds: int = 60
    # 是否仅在交易时段执行；--force 可临时关闭
    trade_time_only: bool = True
    db_path: str = "./fund_valuation.db"
    # 单次 HTTP 超时（秒）
    request_timeout: float = 8.0
    # 批量估值接口单批上限
    batch_size: int = 50
    user_agent: str = DEFAULT_USER_AGENT
    log_level: str = "INFO"
    # 合成路径的并发线程数
    max_workers: int = 4
    # 是否校验 GZTIME 为当日（关掉会写入隔夜旧值，仅调试用）
    validate_gztime: bool = True
    retry_times: int = 2
    # 自定义休市日文件路径，None 表示只用内置表
    holidays_file: str | None = None


@dataclass(slots=True, frozen=True)
class AppConfig:
    """完整配置：基金列表 + 运行时参数 + 来源文件路径（便于日志说明）。"""

    funds: tuple[FundConfig, ...]
    runtime: RuntimeConfig
    source_path: Path | None = None

    def fund_by_code(self, code: str) -> FundConfig | None:
        """按代码查配置。基金数量很少（几十只），线性查找足够。"""
        return next((f for f in self.funds if f.code == code), None)


def load_config(path: str | Path) -> AppConfig:
    """读取并校验 YAML 配置。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"配置文件不存在: {file_path}")

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 语法错误: {exc}") from exc

    if not isinstance(raw, Mapping):
        # 空文件、或根节点写成了列表，都会落到这里。
        raise ConfigError("配置根节点必须是映射（dict），请检查缩进")

    _reject_unknown_keys(raw, _TOP_KEYS, "根节点")

    funds_raw = raw.get("funds")
    if not isinstance(funds_raw, list) or not funds_raw:
        raise ConfigError("funds 必须是非空列表")

    # 逐只校验；索引会带进错误消息，方便直接定位到 YAML 第几个元素。
    funds = tuple(_build_fund(item, index) for index, item in enumerate(funds_raw))
    codes = [f.code for f in funds]
    duplicates = {code for code in codes if codes.count(code) > 1}
    if duplicates:
        # 重复代码会导致同一轮估值互相覆盖（UNIQUE(fund_code, ts)），必须拦住。
        raise ConfigError(f"funds 中存在重复基金代码: {', '.join(sorted(duplicates))}")

    runtime = _build_runtime(raw.get("runtime") or {})
    return AppConfig(funds=funds, runtime=runtime, source_path=file_path)


def _build_fund(item: Any, index: int) -> FundConfig:
    """校验并构造单只基金配置。position 用于错误消息定位。"""
    position = f"funds[{index}]"
    if not isinstance(item, Mapping):
        raise ConfigError(f"{position} 必须是映射（dict）")

    _reject_unknown_keys(item, _FUND_KEYS, position)

    code = str(item.get("code") or "").strip()
    if not code:
        raise ConfigError(f"{position}.code 缺失")
    # 本系统的持仓/净值接口都按 6 位基金代码取值，长度不符必然取不到数据，提前拦住。
    if not (code.isdigit() and len(code) == 6):
        raise ConfigError(f"{position}.code 必须是 6 位数字基金代码，当前为 {code!r}")

    name = item.get("name")
    name = str(name).strip() if name is not None else None

    mode = _parse_enum(FundMode, item.get("mode", FundMode.AUTO), "mode", position)
    holdings_source = _parse_enum(
        HoldingsSource,
        item.get("holdings_source", HoldingsSource.EASTMONEY),
        "holdings_source",
        position,
    )

    max_pct = _to_float(item.get("max_holdings_pct", 0.80), "max_holdings_pct", position)
    # 只接受小数：写成 80 会被拒绝，避免"设了 80 结果全变 high"的误配。
    if not 0 < max_pct <= 1:
        raise ConfigError(
            f"{position}.max_holdings_pct 必须在 (0, 1] 区间内（小数形式，0.8 表示 80%），当前为 {max_pct}"
        )

    fallback_etf = item.get("fallback_etf")
    if fallback_etf in (None, "", "null", "~"):
        # YAML 里写 null/~ 或留空，都归一化为 None（"没有代理"）。
        fallback_etf = None
    else:
        fallback_etf = str(fallback_etf).strip()
        if not fallback_etf:
            raise ConfigError(f"{position}.fallback_etf 不能为空字符串，无代理请写 null")

    return FundConfig(
        code=code,
        name=name,
        mode=mode,
        holdings_source=holdings_source,
        max_holdings_pct=max_pct,
        fallback_etf=fallback_etf,
    )


def _build_runtime(raw: Any) -> RuntimeConfig:
    """逐项校验运行时参数，任意一项非法都立即失败。"""
    if not isinstance(raw, Mapping):
        raise ConfigError("runtime 必须是映射（dict）")
    _reject_unknown_keys(raw, _RUNTIME_KEYS, "runtime")

    defaults = RuntimeConfig()

    interval = _to_int(raw.get("interval_seconds", defaults.interval_seconds), "interval_seconds", "runtime")
    if interval < 1:
        raise ConfigError(f"runtime.interval_seconds 必须 >= 1，当前为 {interval}")

    batch_size = _to_int(raw.get("batch_size", defaults.batch_size), "batch_size", "runtime")
    if not 1 <= batch_size <= 50:
        # 天天基金批量接口实测单批超过 50 只易被截断，故硬性限制。
        raise ConfigError(f"runtime.batch_size 必须在 1..50 之间，当前为 {batch_size}")

    timeout = _to_float(raw.get("request_timeout", defaults.request_timeout), "request_timeout", "runtime")
    if timeout <= 0:
        raise ConfigError(f"runtime.request_timeout 必须 > 0，当前为 {timeout}")

    max_workers = _to_int(raw.get("max_workers", defaults.max_workers), "max_workers", "runtime")
    # 上限 32：再高对几十只基金没有收益，反而更容易触发源站限流。
    if not 1 <= max_workers <= 32:
        raise ConfigError(f"runtime.max_workers 必须在 1..32 之间，当前为 {max_workers}")

    retry_times = _to_int(raw.get("retry_times", defaults.retry_times), "retry_times", "runtime")
    if retry_times < 0:
        raise ConfigError(f"runtime.retry_times 不能为负数，当前为 {retry_times}")

    log_level = str(raw.get("log_level", defaults.log_level)).upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"runtime.log_level 非法: {log_level!r}，允许值: {', '.join(sorted(VALID_LOG_LEVELS))}"
        )

    db_path = str(raw.get("db_path", defaults.db_path))
    if not db_path.strip():
        raise ConfigError("runtime.db_path 不能为空")

    user_agent = str(raw.get("user_agent") or defaults.user_agent)
    # 空字符串视为未配置，退回默认 UA。
    holidays_file = raw.get("holidays_file")
    holidays_file = str(holidays_file).strip() if holidays_file else None

    return RuntimeConfig(
        interval_seconds=interval,
        trade_time_only=_to_bool(raw.get("trade_time_only", defaults.trade_time_only), "trade_time_only"),
        db_path=db_path,
        request_timeout=timeout,
        batch_size=batch_size,
        user_agent=user_agent,
        log_level=log_level,
        max_workers=max_workers,
        validate_gztime=_to_bool(raw.get("validate_gztime", defaults.validate_gztime), "validate_gztime"),
        retry_times=retry_times,
        holidays_file=holidays_file,
    )


def _reject_unknown_keys(raw: Mapping[str, Any], allowed: set[str], position: str) -> None:
    """白名单校验。错误消息同时列出"非法字段"与"允许字段"，便于直接改正。"""
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(
            f"{position} 存在未知字段: {', '.join(sorted(str(k) for k in unknown))}；"
            f"允许的字段: {', '.join(sorted(allowed))}"
        )


def _parse_enum(enum_cls, value: Any, field: str, position: str):
    """按枚举值解析字符串。大小写与空白宽容，但取值必须严格匹配。"""
    text = str(value).strip().lower()
    try:
        return enum_cls(text)
    except ValueError:
        allowed = ", ".join(item.value for item in enum_cls)
        raise ConfigError(
            f"{position}.{field} 的值 {value!r} 非法，允许值: {allowed}"
        ) from None


def _to_float(value: Any, field: str, position: str) -> float:
    """转 float。注意 YAML 里 `"8"`（带引号）也能正确转换，`"8s"` 则报错。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{position}.{field} 必须是数字，当前为 {value!r}") from None


def _to_int(value: Any, field: str, position: str) -> int:
    """转 int，但拒绝 2.5 这类"看起来是整数"的浮点值（静默截断会掩盖配置错误）。"""
    number = _to_float(value, field, position)
    if int(number) != number:
        raise ConfigError(f"{position}.{field} 必须是整数，当前为 {value!r}")
    return int(number)


def _to_bool(value: Any, field: str) -> bool:
    """布尔解析。YAML 原生 bool 直接通过，字符串再按常见写法映射。"""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "on", "1"}:
        return True
    if text in {"false", "no", "off", "0"}:
        return False
    raise ConfigError(f"字段 {field} 必须是布尔值，当前为 {value!r}")
