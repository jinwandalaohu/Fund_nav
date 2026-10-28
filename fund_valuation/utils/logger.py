"""统一日志配置。

所有模块都通过 get_logger(__name__) 取 logger，不自行添加 handler，
这样日志格式与级别只由 setup_logging 一处决定。
"""

from __future__ import annotations

import logging
import sys

_DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """配置根 logger。重复调用不会叠加 handler（便于测试与多次入口调用）。

    实现方式：给本模块添加的 handler 打上自定义标记，重入时先移除旧标记的 handler，
    再挂新的。这样 --once 反复调用或测试里多次 setup 都不会出现重复行。
    """
    numeric_level = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric_level, int):
        # 配置层已校验过级别，这里只是兜底，避免 getattr 返回 None 导致 setLevel 报错。
        numeric_level = logging.INFO

    root = logging.getLogger()
    root.setLevel(numeric_level)

    for handler in list(root.handlers):
        if getattr(handler, "_fund_valuation", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt=_DATE_FORMAT))
    handler._fund_valuation = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # 第三方库的噪声请求日志对排查无益，压到 WARNING。
    # 每个 HTTP 请求都会打一条 urllib3 的 DEBUG，盘中轮询会把有用日志冲掉。
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """取 logger。不在此处配置 handler，级别继承根 logger。"""
    return logging.getLogger(name)
