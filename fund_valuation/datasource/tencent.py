"""腾讯财经行情源。

接口：https://qt.gtimg.cn/q=sh600519,sz000001
返回 JS 赋值语句，形如 `v_sh600519="1~贵州茅台~600519~1408.00~1400.00~...";`
必须用正则解析，绝不能 eval —— 响应来自外网，eval 等于把任意执行权交给对端。

字段用 `~` 分隔，位置固定但没有字段名，因此只取本系统真正需要的三个：
    parts[1]  名称
    parts[3]  最新价
    parts[4]  昨收价
涨跌幅一律由 (现价-昨收)/昨收 现算，不依赖接口自带的涨跌幅字段。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..models import Quote
from ..utils.http import chunked, normalize_code, safe_float
from ..utils.logger import get_logger
from .base import DataSourceError, QuoteSource

LOGGER = get_logger(__name__)

# 匹配 `v_sh600519="...";`，code 允许字母数字下划线（美股代码可能带点，故先宽松匹配）。
_QUOTE_LINE = re.compile(r'v_(?P<code>[A-Za-z0-9_]+)="(?P<body>[^"]*)"')
# 单次请求代码数上限：实测超过 60 只响应会被截断，宁可多请求几次。
_MAX_CODES_PER_REQUEST = 60


class TencentQuoteSource(QuoteSource):
    """行情主源。覆盖 A 股、港股、美股，字段位置在各市场间保持一致。"""

    name = "tencent"
    BASE_URL = "https://qt.gtimg.cn/q="

    def fetch_quotes(self, codes: Sequence[str]) -> dict[str, Quote]:
        """批量取行情。返回的键是归一化代码（sh600519 / hk00700 / usAAPL）。"""
        if not codes:
            return {}

        # 先归一化再去重：持仓里可能同时出现 600519 与 sh600519，避免重复请求。
        # 归一化失败的代码（如填了场外基金代码以外的怪值）跳过而不报错，
        # 单只代码有问题不应影响同批其它股票。
        normalized: dict[str, str] = {}
        for raw in codes:
            try:
                normalized[normalize_code(raw)] = raw
            except ValueError as exc:
                LOGGER.warning("行情代码无法归一化，已跳过: %s (%s)", raw, exc)

        if not normalized:
            return {}

        quotes: dict[str, Quote] = {}
        # sorted 是为了让相同代码集合的请求 URL 稳定，便于排查与缓存。
        for batch in chunked(sorted(normalized), _MAX_CODES_PER_REQUEST):
            try:
                text = self._http.get_text(
                    f"{self.BASE_URL}{','.join(batch)}", encoding="gbk"
                )
            except Exception as exc:  # HttpError 等统一转为 DataSourceError
                raise DataSourceError(self.name, f"行情请求失败: {exc}") from exc
            quotes.update(self._parse(text))

        if not quotes:
            # 解析完一条都没有，说明接口结构变了或整批停牌，属于必须暴露的故障。
            raise DataSourceError(self.name, "行情响应中未解析到任何有效报价")
        return quotes

    def _parse(self, text: str) -> dict[str, Quote]:
        """按正则逐行解析 JS 赋值语句。不解析的行直接跳过（接口偶有占位行）。"""
        result: dict[str, Quote] = {}
        for match in _QUOTE_LINE.finditer(text):
            code = match.group("code").lower()
            parts = match.group("body").split("~")
            if len(parts) < 6:
                # 字段数不足说明该行不是有效行情（如 `v_pv_none_match="1";`）。
                continue

            price = safe_float(parts[3])
            prev_close = safe_float(parts[4])
            if price is None or prev_close is None or price <= 0 or prev_close <= 0:
                # 停牌或未开盘时常返回 0，这类数据不能参与加权计算。
                LOGGER.debug("跳过无效报价: %s price=%s prev=%s", code, price, prev_close)
                continue

            # parts[32] 是接口自带的涨跌幅，仅作排查参考，不参与加权。
            hint = safe_float(parts[32]) if len(parts) > 32 else None
            result[code] = Quote(
                asset_code=code,
                price=price,
                prev_close=prev_close,
                name=parts[1] if len(parts) > 1 else "",
                change_pct_hint=hint,
                ts=parts[30] if len(parts) > 30 else None,
            )
        return result
