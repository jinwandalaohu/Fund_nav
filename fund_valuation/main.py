"""入口：加载配置、组装依赖、启动调度或单次计算。

用法：
    python -m fund_valuation.main --config fund_config.yml            # 常驻轮询
    python -m fund_valuation.main --config fund_config.yml --once     # 手动跑一次
    python -m fund_valuation.main --config fund_config.yml --once --show

启动顺序与失败语义：
    配置加载失败 → 打印错误并返回退出码 2（不进入任何网络请求）
    数据库打不开   → 抛异常退出（没有库就没有意义）
    单轮估值异常   → 常驻模式记录后继续；--once 模式同样不抛（引擎已内部隔离）
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .datasource.registry import (
    build_holdings_source,
    build_nav_source,
    build_quote_sources,
    build_valuation_sources,
)
from .db import Database
from .scheduler import Scheduler
from .utils.calendar import TradingCalendar
from .utils.http import HttpClient
from .utils.logger import get_logger, setup_logging
from .valuation.direct import DirectValuator
from .valuation.engine import ValuationEngine
from .valuation.synthetic import SyntheticValuator

LOGGER = get_logger(__name__)

DEFAULT_CONFIG = "fund_config.yml"


def build_engine(config: AppConfig, db: Database) -> ValuationEngine:
    """依赖装配：所有超时/重试/UA 集中在一个 HttpClient 实例。

    只创建一个 HttpClient 是为了让超时、重试次数、User-Agent 只有一个来源；
    它内部按线程隔离 Session，因此可安全地被并发使用。
    """
    runtime = config.runtime
    http = HttpClient(
        timeout=runtime.request_timeout,
        retries=runtime.retry_times,
        user_agent=runtime.user_agent,
    )

    # 直接路径：源链顺序固定为 天天基金 → 新浪（批量优先）。
    direct = DirectValuator(build_valuation_sources(http), runtime)

    # 合成路径：持仓源按配置决定主备顺序，行情源固定 腾讯 → 新浪，
    # snapshot_loader 注入数据库读取方法，实现"24 小时内复用快照"。
    synthetic = SyntheticValuator(
        holdings_sources=build_holdings_source(http, _primary_holdings_source(config)),
        quote_sources=build_quote_sources(http),
        nav_source=build_nav_source(http),
        snapshot_loader=db.latest_holdings,
    )
    return ValuationEngine(config=config, db=db, direct=direct, synthetic=synthetic)


def _primary_holdings_source(config: AppConfig):
    """持仓源以配置中出现次数最多的取值为默认，单只基金可在 synthetic 内部按需切换。

    注意这是"多数票"而非逐基金生效：当配置里同时存在 eastmoney 与 tiantian 时，
    少数派的取值不会改变源链顺序（另一个源仍作为备份存在，只是不再是主源）。
    """
    counts: dict[str, int] = {}
    for fund in config.funds:
        counts[fund.holdings_source] = counts.get(fund.holdings_source, 0) + 1
    return max(counts, key=counts.get)


def print_recent(db: Database, limit: int = 20) -> None:
    """控制台打印最近若干条估值，便于快速核对。

    列宽按内容自适应，并用 CJK 宽度计算对齐 —— 否则中英文混排的表格会错位。
    """
    rows = db.recent_valuations(limit=limit)
    if not rows:
        print("（数据库中暂无估值记录）")
        return

    headers = ["时间", "基金", "涨跌%", "估算净值", "覆盖率", "置信度", "source", "状态"]
    table: list[list[str]] = []
    for row in rows:
        change = row["estimated_change_pct"]
        coverage = row["coverage_pct"]
        table.append(
            [
                row["ts"],
                row["fund_code"],
                f"{change:+.2f}" if change is not None else "--",
                f"{row['estimated_nav']:.4f}" if row["estimated_nav"] is not None else "--",
                # coverage_pct 存的是小数，展示时转百分数。
                f"{coverage:.1%}" if coverage is not None else "--",
                row["confidence"] or "--",
                row["source"] or "--",
                row["status"],
            ]
        )

    widths = [
        max(_display_width(cells[i]) for cells in [headers, *table])
        for i in range(len(headers))
    ]
    # 涨跌、净值、覆盖率三列右对齐，便于纵向比对数量级。
    numeric = {2, 3, 4}

    def render(cells: list[str]) -> str:
        """把一行单元格按列宽拼成字符串，末尾空格去掉避免行尾空白。"""
        return "  ".join(
            _pad(cell, widths[i], right=i in numeric) for i, cell in enumerate(cells)
        ).rstrip()

    line = render(headers)
    print(line)
    print("-" * _display_width(line))
    for cells in table:
        print(render(cells))


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 字符占 2 列，否则中文列会错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    """按显示宽度补齐空格。right=True 时左补（右对齐）。"""
    padding = " " * max(0, width - _display_width(text))
    return f"{padding}{text}" if right else f"{text}{padding}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。默认值即可直接运行（读同目录的 fund_config.yml）。"""
    parser = argparse.ArgumentParser(description="基金盘中实时估值系统")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="YAML 配置路径（默认 fund_config.yml）")
    parser.add_argument("--once", action="store_true", help="只执行一次估值后退出（忽略交易时段限制）")
    parser.add_argument("--iterations", type=int, default=None, help="常驻模式下最多轮询次数（调试用）")
    parser.add_argument("--force", action="store_true", help="忽略 trade_time_only，非交易时段也执行")
    parser.add_argument("--show", action="store_true", help="运行结束后打印最近估值记录")
    parser.add_argument("--show-limit", type=int, default=20, help="--show 打印条数（默认 20）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """程序入口，返回进程退出码（0 成功 / 2 配置错误）。"""
    args = parse_args(argv)
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        # 配置错误时日志系统还没按配置初始化，先临时设为 ERROR 保证错误可见。
        setup_logging("ERROR")
        LOGGER.error("配置错误: %s", exc)
        return 2

    setup_logging(config.runtime.log_level)
    if args.force and config.runtime.trade_time_only:
        # 用 replace 生成新配置而不是就地改字段：RuntimeConfig 是 frozen 的，
        # 也保证"原始配置"与"实际生效配置"可区分。
        config = replace(config, runtime=replace(config.runtime, trade_time_only=False))
        LOGGER.info("--force 已启用：忽略交易时段限制")

    db = Database(config.runtime.db_path)
    LOGGER.info("数据库: %s | 基金数: %d", db.path, len(config.funds))

    try:
        engine = build_engine(config, db)
        if args.once:
            # 单次模式：直接跑一轮，不等交易时段（便于盘后补算与调试）。
            engine.run_once()
        else:
            scheduler = Scheduler(engine, config, TradingCalendar(config.runtime.holidays_file))
            scheduler.run_forever(max_iterations=args.iterations)

        if args.show:
            print_recent(db, args.show_limit)
    finally:
        # 无论正常结束还是异常，都保证连接关闭、WAL 落盘。
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
