"""数据源工厂：把配置里的字符串映射到具体实现。

新增数据源只需：实现 base.py 的抽象类 → 在下面的 match 分支里登记。

装配关系（源链的顺序即优先级）：
    直接估值  tiantian(批量) → sina(单只补齐)
    个股行情  tencent       → sina(A 股)
    基金持仓  eastmoney(移动端) ↔ tiantian(档案接口)   互为备份
    基准净值  tiantian(pingzhongdata → lsjz)
"""

from __future__ import annotations

from typing import Sequence

from ..models import FundMode
from ..models import HoldingsSource as HoldingsSourceName
from ..utils.http import HttpClient
from ..utils.logger import get_logger
from .base import HoldingsSource, NavSource, QuoteSource, ValuationSource
from .eastmoney import EastmoneyHoldingsSource
from .sina import SinaQuoteSource, SinaValuationSource
from .tencent import TencentQuoteSource
from .tiantian import TiantianHoldingsSource, TiantianNavSource, TiantianValuationSource

LOGGER = get_logger(__name__)


def build_valuation_sources(http: HttpClient, names: Sequence[str] | None = None) -> list[ValuationSource]:
    """按优先级构造直接估值源链。默认：天天基金 → 新浪。

    顺序有实际意义：链条前者能批量取数，后者只能单只，
    所以天天基金必须在前，否则会把批量优势浪费掉。
    """
    order = list(names or ["tiantian", "sina"])
    sources: list[ValuationSource] = []
    for name in order:
        match name:
            case "tiantian":
                sources.append(TiantianValuationSource(http))
            case "sina":
                sources.append(SinaValuationSource(http))
            case _:
                # 未知名字只告警不抛错：由下方"至少一个源"的统一校验兜底。
                LOGGER.warning("未知的直接估值源，已忽略: %s", name)
    if not sources:
        raise ValueError("未配置任何可用的直接估值源")
    return sources


def build_quote_sources(http: HttpClient, names: Sequence[str] | None = None) -> list[QuoteSource]:
    """按优先级构造行情源链。默认：腾讯 → 新浪。

    腾讯覆盖港股/美股且一次可带 60 只，故作为主源；新浪只覆盖 A 股，作为备份。
    """
    order = list(names or ["tencent", "sina"])
    sources: list[QuoteSource] = []
    for name in order:
        match name:
            case "tencent":
                sources.append(TencentQuoteSource(http))
            case "sina":
                sources.append(SinaQuoteSource(http))
            case _:
                LOGGER.warning("未知的行情源，已忽略: %s", name)
    if not sources:
        raise ValueError("未配置任何可用的行情源")
    return sources


def build_holdings_source(
    http: HttpClient,
    name: HoldingsSourceName | str,
    *,
    preferred: HoldingsSource | None = None,
    fallback: HoldingsSource | None = None,
) -> list[HoldingsSource]:
    """构造持仓源链：配置指定的源优先，另一个作为备份。

    preferred / fallback 仅用于测试注入假实现；生产路径按 name 决定顺序。
    """
    resolved = HoldingsSourceName(str(name))
    match resolved:
        case HoldingsSourceName.EASTMONEY:
            primary: HoldingsSource = EastmoneyHoldingsSource(http)
            backup: HoldingsSource = TiantianHoldingsSource(http)
        case HoldingsSourceName.TIANTIAN:
            primary = TiantianHoldingsSource(http)
            backup = EastmoneyHoldingsSource(http)
        case _:  # pragma: no cover - 枚举已被 config 校验
            raise ValueError(f"未知持仓源: {name}")

    if preferred is not None:
        primary = preferred
    if fallback is not None:
        backup = fallback
    return [primary, backup]


def build_nav_source(http: HttpClient) -> NavSource:
    """基准净值源目前只有天天基金一条通道（内含 pingzhongdata/lsjz 双策略）。"""
    return TiantianNavSource(http)


def needs_direct(mode: FundMode) -> bool:
    """该模式是否需要走直接取值路径。synthetic 模式无需发起批量估值请求。"""
    return mode in (FundMode.AUTO, FundMode.DIRECT)
