"""SQLite3 持久化层。

对外只暴露高层方法（save_valuation / save_holdings_snapshot ...），SQL 全部集中在本模块，
上层不出现任何 SQL 字符串，换存储引擎时只改这里。

并发策略：WAL 支持"多读一写"，但 sqlite3 连接对象不是线程安全的，
因此引擎虽然并发计算，落库统一在主线程串行执行，并用锁保护连接。

三张表的分工：
    funds              基金基础信息，一只一行（名称、类型、最后持仓更新时间）
    holdings_snapshot  持仓明细，一只基金一次抓取写 10 行，保留历史供审计
    valuation          估值结果，盘中的核心产出，按 (fund_code, ts) 幂等
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import Holding, HoldingsSnapshot, ValuationResult
from .utils.logger import get_logger

LOGGER = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    fund_code             TEXT PRIMARY KEY,
    fund_name             TEXT,
    fund_type             TEXT,
    last_holdings_update  TEXT,
    UNIQUE(fund_code)
);

CREATE TABLE IF NOT EXISTS holdings_snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code      TEXT NOT NULL,
    snapshot_time  TEXT NOT NULL,
    asset_code     TEXT NOT NULL,
    asset_name     TEXT,
    weight         REAL,
    asset_type     TEXT DEFAULT 'stock',
    report_date    TEXT
);

CREATE TABLE IF NOT EXISTS valuation (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code            TEXT NOT NULL,
    ts                   TEXT NOT NULL,
    estimated_nav        REAL,
    estimated_change_pct REAL,
    base_nav             REAL,
    coverage_pct         REAL,
    confidence           TEXT,
    source               TEXT,
    status               TEXT,
    data_source          TEXT,
    error_msg            TEXT,
    UNIQUE(fund_code, ts)
);

CREATE INDEX IF NOT EXISTS idx_valuation_code_ts ON valuation(fund_code, ts);
CREATE INDEX IF NOT EXISTS idx_valuation_ts ON valuation(ts);
CREATE INDEX IF NOT EXISTS idx_holdings_code_time ON holdings_snapshot(fund_code, snapshot_time);
"""

# 幂等写入：同一基金同一秒重复计算时更新而非新增。
# 这也意味着"同秒两次不同结果只保留最后一次"，因此 ts 必须是秒级（见 timeutil.iso_timestamp）。
_INSERT_VALUATION = """
INSERT INTO valuation (
    fund_code, ts, estimated_nav, estimated_change_pct, base_nav,
    coverage_pct, confidence, source, status, data_source, error_msg
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(fund_code, ts) DO UPDATE SET
    estimated_nav        = excluded.estimated_nav,
    estimated_change_pct = excluded.estimated_change_pct,
    base_nav             = excluded.base_nav,
    coverage_pct         = excluded.coverage_pct,
    confidence           = excluded.confidence,
    source               = excluded.source,
    status               = excluded.status,
    data_source          = excluded.data_source,
    error_msg            = excluded.error_msg
"""


class Database:
    """估值库封装。实例可跨线程使用（内部加锁）。"""

    def __init__(self, path: str | Path) -> None:
        """打开（或创建）数据库并完成初始化。传 ":memory:" 可得到内存库用于测试。"""
        self.path = str(path)
        if self.path != ":memory:":
            # 目录不存在时自动创建，避免首次运行因路径问题失败。
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

        # RLock 而非 Lock：save_holdings_snapshot 内部会连续执行多条语句，
        # 若将来出现嵌套调用，RLock 不会自锁死。
        self._lock = threading.RLock()
        # check_same_thread=False 允许跨线程复用连接，配合 _lock 保证串行。
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        # row_factory=Row：查询结果可按列名取值，避免依赖列顺序。
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.initialize()

    # ------------------------------------------------------------------ 基础
    def _configure(self) -> None:
        """连接级 PRAGMA。每个连接都要设，不是库级配置。"""
        with self._lock, self._conn:
            # WAL：允许估值查询与写入并发，避免读写互相阻塞。
            self._conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL：WAL 下足够安全，且不必每次提交都 fsync，写入快很多。
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # 写锁等待 5 秒：并发写时先等待而不是立刻抛 "database is locked"。
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def initialize(self) -> None:
        """建表 + 轻量迁移。首次运行自动创建，无需手工 SQL。"""
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """为旧库补充 report_date 列（用于记录持仓报告期）。

        SQLite 没有 "ADD COLUMN IF NOT EXISTS"，因此先查 table_info 再决定是否 ALTER。
        """
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(holdings_snapshot)").fetchall()
        }
        if "report_date" not in columns:
            self._conn.execute("ALTER TABLE holdings_snapshot ADD COLUMN report_date TEXT")
            LOGGER.info("holdings_snapshot 已补充 report_date 列")

    def close(self) -> None:
        """关闭连接。WAL 模式下关闭时会把 -wal 内容合并回主库。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        """支持 with Database(...) as db，退出时自动关闭。"""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """退出上下文即关闭连接；不吞异常，异常照常向上抛。"""
        self.close()

    # ------------------------------------------------------------------ 写入
    def save_valuation(self, result: ValuationResult) -> None:
        """写单条估值。写失败只记日志，不向上抛 —— 落库故障不应中断整批任务。"""
        try:
            with self._lock, self._conn:
                self._conn.execute(_INSERT_VALUATION, result.as_row())
        except sqlite3.Error as exc:
            LOGGER.error("写入估值失败 %s: %s", result.fund_code, exc)

    def save_valuations(self, results: Iterable[ValuationResult]) -> None:
        """批量写估值（executemany，一次事务）。一轮轮询只写一次盘。"""
        rows = [r.as_row() for r in results]
        if not rows:
            return
        try:
            with self._lock, self._conn:
                self._conn.executemany(_INSERT_VALUATION, rows)
        except sqlite3.Error as exc:
            LOGGER.error("批量写入估值失败: %s", exc)

    def save_holdings_snapshot(self, snapshot: HoldingsSnapshot) -> None:
        """保存持仓快照。同一 (fund_code, snapshot_time) 重复写入时先清后插，保持幂等。

        不用 UNIQUE 约束而用"先删后插"：一条快照是 10 行，
        删掉整组再整组写入比逐行 upsert 更简单，也不会留下上一版的残留行。
        """
        rows = [
            (
                snapshot.fund_code,
                snapshot.snapshot_time,
                holding.asset_code,
                holding.asset_name,
                holding.weight,
                # asset_type 可能是枚举也可能是 str（从库里读回来时是 str）。
                holding.asset_type.value if hasattr(holding.asset_type, "value") else holding.asset_type,
                snapshot.report_date,
            )
            for holding in snapshot.holdings
        ]
        if not rows:
            return
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM holdings_snapshot WHERE fund_code = ? AND snapshot_time = ?",
                    (snapshot.fund_code, snapshot.snapshot_time),
                )
                self._conn.executemany(
                    """
                    INSERT INTO holdings_snapshot
                        (fund_code, snapshot_time, asset_code, asset_name, weight, asset_type, report_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                # 同步更新 funds.last_holdings_update，供运维判断"持仓多久没更新了"。
                self._conn.execute(
                    "UPDATE funds SET last_holdings_update = ? WHERE fund_code = ?",
                    (snapshot.snapshot_time, snapshot.fund_code),
                )
        except sqlite3.Error as exc:
            LOGGER.error("写入持仓快照失败 %s: %s", snapshot.fund_code, exc)

    def upsert_fund(
        self,
        fund_code: str,
        fund_name: str | None = None,
        fund_type: str | None = None,
    ) -> None:
        """写入/更新基金基础信息。

        用 COALESCE 而非直接覆盖：本次若没拿到名称（传 None），应保留库里已有的名称，
        而不是把已有信息清空。
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO funds (fund_code, fund_name, fund_type)
                    VALUES (?, ?, ?)
                    ON CONFLICT(fund_code) DO UPDATE SET
                        fund_name = COALESCE(excluded.fund_name, funds.fund_name),
                        fund_type = COALESCE(excluded.fund_type, funds.fund_type)
                    """,
                    (fund_code, fund_name, fund_type),
                )
        except sqlite3.Error as exc:
            LOGGER.error("写入基金基础信息失败 %s: %s", fund_code, exc)

    # ------------------------------------------------------------------ 读取
    def latest_holdings(self, fund_code: str, max_age_hours: float = 24.0) -> HoldingsSnapshot | None:
        """取指定时长内最新的一份持仓快照；无有效快照返回 None。

        该方法被注入给 SyntheticValuator 作为 snapshot_loader，
        因此签名必须与 Callable[[str, float], HoldingsSnapshot | None] 一致。
        """
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).replace(microsecond=0).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM holdings_snapshot
                WHERE fund_code = ? AND snapshot_time >= ?
                ORDER BY snapshot_time DESC, id ASC
                """,
                (fund_code, cutoff),
            ).fetchall()

        if not rows:
            return None

        # 第一行的时间就是最新快照时间，只取同一时刻的行（一次快照 = 一组 10 行）。
        latest_time = rows[0]["snapshot_time"]
        holdings = tuple(
            Holding(
                asset_code=row["asset_code"],
                asset_name=row["asset_name"] or "",
                weight=float(row["weight"] or 0.0),
                asset_type=row["asset_type"] or "stock",
            )
            for row in rows
            if row["snapshot_time"] == latest_time
        )
        if not holdings:
            return None
        return HoldingsSnapshot(
            fund_code=fund_code,
            holdings=holdings,
            snapshot_time=latest_time,
            report_date=rows[0]["report_date"],
            # source 标成 db:snapshot，落库后可从 data_source 字段看出"这次用的是缓存"。
            source="db:snapshot",
        )

    def recent_valuations(self, limit: int = 20, fund_code: str | None = None) -> list[dict[str, Any]]:
        """最近若干条估值，倒序。供 --show 与人工排查使用。"""
        query = "SELECT * FROM valuation"
        params: list[Any] = []
        if fund_code:
            query += " WHERE fund_code = ?"
            params.append(fund_code)
        # 同一秒可能有多条（不同基金），用 id 兜底保证顺序稳定。
        query += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [dict(row) for row in self._conn.execute(query, params).fetchall()]

    def distribution(self, hours: int = 24) -> dict[str, dict[str, int]]:
        """统计近 N 小时的 source / confidence / status 分布，用于健康度观察。

        三个维度先按组合 GROUP BY 再在内存里展开，SQL 只查一次。
        """
        cutoff = (datetime.now() - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT status, source, confidence, COUNT(*) AS cnt
                FROM valuation WHERE ts >= ?
                GROUP BY status, source, confidence
                """,
                (cutoff,),
            ).fetchall()

        summary: dict[str, dict[str, int]] = {"status": {}, "source": {}, "confidence": {}}
        for row in rows:
            count = int(row["cnt"])
            summary["status"][row["status"]] = summary["status"].get(row["status"], 0) + count
            # source / confidence 可能为 NULL（failed 行），不计入分布。
            if row["source"]:
                summary["source"][row["source"]] = summary["source"].get(row["source"], 0) + count
            if row["confidence"]:
                summary["confidence"][row["confidence"]] = (
                    summary["confidence"].get(row["confidence"], 0) + count
                )
        return summary
