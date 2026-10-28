"""估值调度引擎：双轨选择、降级、落库、汇总。

职责边界：只做"策略编排"，不关心 HTTP 细节、不写 SQL、不解析 HTML。
新增策略只需在这里挂一个新的 match 分支。

一次 run_once 的完整链路：
    ① _ensure_funds   把配置里的基金写入 funds 表（供后续关联查询）
    ② _run_direct     一次批量请求取所有 auto/direct 基金的 GSZ/GSZZL
    ③ _evaluate_all   线程池并发，按 mode 决定"直接用"还是"降级合成"
    ④ _persist        持仓快照 + 估值结果落库
    ⑤ 汇总日志        总数/成功/无估值/失败/耗时/source 分布

为什么②必须先整体跑完：批量接口一次 HTTP 就能覆盖全部基金，若放进线程池
按基金逐个请求，会把 1 次请求放大成 N 次，直接触发源站限流。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from ..config import AppConfig
from ..datasource.base import DataSourceError
from ..db import Database
from ..models import (
    FundConfig,
    FundMode,
    HoldingsSnapshot,
    RunSummary,
    ValuationResult,
    ValuationStatus,
)
from ..utils.html import guess_fund_type
from ..utils.logger import get_logger
from ..utils.timeutil import iso_timestamp
from .direct import DirectValuator
from .synthetic import SyntheticValuator

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class FundOutcome:
    """单只基金的计算产物：结果 + 需要落库的持仓快照。"""

    result: ValuationResult
    snapshot: HoldingsSnapshot | None = None


class ValuationEngine:
    """双轨估值引擎。持有配置、数据库与两个估值器，是唯一的编排入口。"""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        direct: DirectValuator,
        synthetic: SyntheticValuator,
    ) -> None:
        """依赖全部由 main.build_engine 注入，引擎内部不新建任何数据源或连接。"""
        self._config = config
        self._db = db
        self._direct = direct
        self._synthetic = synthetic
        # 并发度不能超过基金数，否则线程白开；下限 1 保证串行也能工作。
        self._workers = max(1, config.runtime.max_workers)

    # ------------------------------------------------------------------ 主流程
    def run_once(self, moment: datetime | None = None) -> RunSummary:
        """执行一轮完整估值。任何单只基金的失败都不会中断本轮。"""
        started = time.perf_counter()
        # 去微秒：与 valuation.ts 的秒级格式一致，避免同秒重复写入变成两条记录。
        now = (moment or datetime.now()).replace(microsecond=0)

        self._ensure_funds()
        direct_results = self._run_direct(now)
        outcomes = self._evaluate_all(now, direct_results)
        self._persist(outcomes)

        summary = RunSummary.from_results(
            [outcome.result for outcome in outcomes], time.perf_counter() - started
        )
        self._log_summary(summary)
        return summary

    # ------------------------------------------------------------------ 直接取值
    def _run_direct(self, now: datetime) -> dict[str, ValuationResult]:
        """批量取直接估值。只覆盖 auto / direct 模式的基金。

        synthetic 模式的基金完全不需要这次请求，因此不放进 codes，
        既省流量也避免它们的代码污染批量接口。
        """
        codes = [f.code for f in self._config.funds if f.mode in (FundMode.AUTO, FundMode.DIRECT)]
        if not codes:
            return {}
        try:
            return self._direct.evaluate(codes, now)
        except Exception as exc:  # 直接路径整体异常时仍应继续走合成
            # 兜底到"本轮没有直接估值"，auto 模式的基金会全部走合成路径，
            # 不至于因为一个接口故障导致整轮空转。
            LOGGER.exception("直接估值路径异常，全部降级处理: %s", exc)
            return {}

    # ------------------------------------------------------------------ 单只基金
    def _evaluate_all(
        self,
        now: datetime,
        direct_results: dict[str, ValuationResult],
    ) -> list[FundOutcome]:
        """并发计算所有基金。返回顺序与配置顺序一致，便于日志与排查比对。"""
        funds = list(self._config.funds)
        if len(funds) == 1:
            # 只有一只时不走线程池：省掉线程创建与上下文切换的开销。
            return [self._evaluate_one(funds[0], now, direct_results.get(funds[0].code))]

        outcomes: dict[str, FundOutcome] = {}
        with ThreadPoolExecutor(max_workers=min(self._workers, len(funds))) as pool:
            # 用 fund 作为 value，便于 in-flight 时把异常映射回具体基金。
            futures = {
                pool.submit(self._evaluate_one, fund, now, direct_results.get(fund.code)): fund
                for fund in funds
            }
            for future, fund in futures.items():
                try:
                    outcomes[fund.code] = future.result()
                except Exception as exc:  # 线程内异常不能丢，转成 failed 记录
                    # 线程里的异常不会自动冒泡到主线程，必须在这里显式转成 failed，
                    # 否则该基金在库里会"凭空消失"。
                    LOGGER.exception("基金 %s 估值出现未预期异常", fund.code)
                    outcomes[fund.code] = FundOutcome(
                        result=self._failed_result(fund.code, now, f"未预期异常: {exc}")
                    )

        # 按配置顺序输出（outcomes 的插入顺序取决于线程完成顺序，不可依赖）。
        return [outcomes[fund.code] for fund in funds if fund.code in outcomes]

    def _evaluate_one(
        self,
        fund: FundConfig,
        now: datetime,
        direct_result: ValuationResult | None,
    ) -> FundOutcome:
        """按 mode 分派。三种模式的差异只在"是否接受直接结果"。"""
        match fund.mode:
            case FundMode.DIRECT:
                # 用户明确要求只看接口值：没有就直接失败，不静默降级到合成。
                if direct_result is None:
                    return FundOutcome(
                        result=self._failed_result(fund.code, now, "direct 模式未获得任何估值数据")
                    )
                return FundOutcome(result=direct_result)

            case FundMode.SYNTHETIC:
                # 强制合成，忽略已拿到的直接估值。
                return self._try_synthetic(fund, now, direct_result=None)

            case FundMode.AUTO:
                # 双轨核心：直接取值可用就直接用，否则（无估值/隔夜旧值/失败）降级。
                if direct_result and direct_result.status is ValuationStatus.AVAILABLE:
                    return FundOutcome(result=direct_result)
                return self._try_synthetic(fund, now, direct_result=direct_result)

            case _:  # pragma: no cover - 枚举已被配置校验
                return FundOutcome(
                    result=self._failed_result(fund.code, now, f"未知估值模式: {fund.mode}")
                )

    def _try_synthetic(
        self,
        fund: FundConfig,
        now: datetime,
        *,
        direct_result: ValuationResult | None,
    ) -> FundOutcome:
        """调用合成路径，并把"两段失败原因"合并进 error_msg。

        direct_result 只用于拼装错误信息（为什么走到了合成、以及合成又为什么失败），
        不参与任何计算 —— 合成结果与直接估值是两条独立的计算链路。
        """
        try:
            outcome = self._synthetic.evaluate(fund, now)
        except DataSourceError as exc:
            reason = exc.message
            # 拼接上游原因：只说"synthetic 失败"无法判断是持仓问题还是行情问题，
            # 也看不出为何没走直接取值。
            if direct_result is not None and direct_result.error_msg:
                reason = f"direct: {direct_result.error_msg}; synthetic: {reason}"
            elif direct_result is not None and direct_result.status is ValuationStatus.NOT_SUPPORTED:
                reason = f"direct: 接口无实时估值; synthetic: {reason}"
            LOGGER.warning("%s 估值失败(%s): %s", fund.code, fund.mode.value, reason)
            return FundOutcome(result=self._failed_result(fund.code, now, reason))
        except Exception as exc:  # 兜底，避免单只基金拖垮整批
            # 非数据源异常属于代码缺陷，打印完整堆栈。
            LOGGER.exception("%s 合成路径未预期异常", fund.code)
            return FundOutcome(result=self._failed_result(fund.code, now, f"未预期异常: {exc}"))

        if direct_result is not None and direct_result.error_msg:
            # 降级成功也要留痕：这是"数据可用但不权威"的信号。
            LOGGER.debug("%s 已降级为合成估值（%s）", fund.code, direct_result.error_msg)
        return outcome

    # ------------------------------------------------------------------ 落库
    def _ensure_funds(self) -> None:
        """把配置中的基金写入 funds 表。

        先建行再写估值，保证 valuation 里的 fund_code 在 funds 中总能找到对应记录，
        不会出现"孤儿估值"。fund_type 由名称推断，仅用于展示。
        """
        for fund in self._config.funds:
            self._db.upsert_fund(fund.code, fund.name, guess_fund_type(fund.name))

    def _persist(self, outcomes: Iterable[FundOutcome]) -> None:
        """持久化：持仓快照 + 基金名 + 估值结果。

        顺序有意为之：先写快照与基金信息，最后批量写估值 ——
        这样即使估值写入批量失败，快照与基金信息也不会回滚（便于事后重算）。
        """
        results: list[ValuationResult] = []
        for outcome in outcomes:
            results.append(outcome.result)
            if outcome.snapshot is not None and outcome.snapshot.holdings:
                # 只有真正抓了新持仓才写库；复用快照时不重复写，避免快照表膨胀。
                self._db.save_holdings_snapshot(outcome.snapshot)
            # 直接路径没有配置里的名称时，用接口返回的 fund_name 补全。
            name = outcome.result.fund_name or self._fund_name(outcome.result.fund_code)
            self._db.upsert_fund(outcome.result.fund_code, name, guess_fund_type(name))
        self._db.save_valuations(results)

    def _fund_name(self, fund_code: str) -> str | None:
        """从配置里取基金名称，用于 fallback 的情况下补全 funds 表。"""
        fund = self._config.fund_by_code(fund_code)
        return fund.name if fund else None

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _failed_result(fund_code: str, now: datetime, message: str) -> ValuationResult:
        """构造失败结果：只有状态与错误信息，所有数值字段为 NULL。"""
        return ValuationResult(
            fund_code=fund_code,
            ts=iso_timestamp(now),
            status=ValuationStatus.FAILED,
            error_msg=message,
        )

    @staticmethod
    def _log_summary(summary: RunSummary) -> None:
        """输出一行汇总日志。source/confidence 分布是判断系统健康度的主要指标。"""
        sources = ",".join(f"{k}={v}" for k, v in sorted(summary.by_source.items())) or "-"
        confidences = ",".join(f"{k}={v}" for k, v in sorted(summary.by_confidence.items())) or "-"
        LOGGER.info(
            "轮询完成: 共 %d 只 | 成功 %d | 无估值 %d | 失败 %d | 耗时 %.1fs | source[%s] confidence[%s]",
            summary.total,
            summary.available,
            summary.not_supported,
            summary.failed,
            summary.elapsed_seconds,
            sources,
            confidences,
        )
