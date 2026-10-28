"""新浪财经数据源：个股行情（腾讯的备用）与基金估值曲线（天天基金的备用）。

两个注意点：
1. 行情接口返回 GBK，必须显式设 encoding='gbk'，否则中文名乱码。
2. 自 2021 年起 hq.sinajs.cn 校验 Referer，缺失会返回 403 或空串。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..models import DirectQuote, Quote
from ..utils.http import chunked, normalize_code, safe_float
from ..utils.logger import get_logger
from .base import DataSourceError, QuoteSource, ValuationSource

LOGGER = get_logger(__name__)

_QUOTE_LINE = re.compile(r'var hq_str_(?P<code>[a-zA-Z0-9]+)="(?P<body>[^"]*)"')
_MAX_CODES_PER_REQUEST = 50

# 新浪 A 股行情格式：名称,今开,昨收,现价,最高,最低,...
# 只取这三个位置：昨收(2) 与现价(3) 用于自算涨跌幅，名称(0) 仅用于日志。
_IDX_NAME = 0
_IDX_PREV_CLOSE = 2
_IDX_PRICE = 3

# 只支持沪深京三市：这三个市场的逗号分隔字段口径一致。
SUPPORTED_PREFIXES = ("sh", "sz", "bj")


class SinaQuoteSource(QuoteSource):
    """行情备源。字段口径与腾讯不同（逗号分隔），因此单独一套下标常量。"""

    name = "sina"
    BASE_URL = "https://hq.sinajs.cn/list="
    HEADERS = {"Referer": "https://finance.sina.com.cn/"}

    def fetch_quotes(self, codes: Sequence[str]) -> dict[str, Quote]:
        """批量取行情。不支持的市场的代码静默跳过，由腾讯源兜住。"""
        if not codes:
            return {}

        normalized: list[str] = []
        for raw in codes:
            try:
                code = normalize_code(raw)
            except ValueError as exc:
                LOGGER.warning("行情代码无法归一化，已跳过: %s (%s)", raw, exc)
                continue
            if not code.startswith(SUPPORTED_PREFIXES):
                # 港股/美股的新浪行情字段口径与 A 股不同，交给腾讯源处理，
                # 这里静默跳过而不是猜测字段位置（猜错会算出错误涨跌幅）。
                LOGGER.debug("新浪源暂不支持该市场，跳过: %s", code)
                continue
            normalized.append(code)

        if not normalized:
            # 全是港股/美股时返回空字典而不是抛异常：这不是故障，
            # 只是因为本源不覆盖该市场，上层会继续用其它源。
            return {}

        quotes: dict[str, Quote] = {}
        for batch in chunked(sorted(set(normalized)), _MAX_CODES_PER_REQUEST):
            try:
                text = self._http.get_text(
                    f"{self.BASE_URL}{','.join(batch)}",
                    headers=self.HEADERS,
                    encoding="gbk",
                )
            except Exception as exc:
                raise DataSourceError(self.name, f"行情请求失败: {exc}") from exc
            quotes.update(self._parse(text))

        if not quotes:
            raise DataSourceError(self.name, "行情响应中未解析到任何有效报价")
        return quotes

    def _parse(self, text: str) -> dict[str, Quote]:
        """解析 `var hq_str_sh600519="名称,今开,昨收,现价,...";`"""
        result: dict[str, Quote] = {}
        for match in _QUOTE_LINE.finditer(text):
            code = match.group("code").lower()
            parts = match.group("body").split(",")
            if len(parts) <= _IDX_PRICE:
                # 停牌或代码不存在时 body 为空串，字段数不足直接跳过。
                continue
            price = safe_float(parts[_IDX_PRICE])
            prev_close = safe_float(parts[_IDX_PREV_CLOSE])
            if not price or not prev_close or price <= 0 or prev_close <= 0:
                continue
            result[code] = Quote(
                asset_code=code,
                price=price,
                prev_close=prev_close,
                name=parts[_IDX_NAME],
            )
        return result


class SinaValuationSource(ValuationSource):
    """新浪基金估值曲线，取序列最后一点作为当前估算。

    与天天基金批量接口的差别：
    - 只能单只查询（没有批量参数），因此放在源链第二位，仅用于补齐缺失代码；
    - 只给涨跌幅 growthrate 与基准净值 pre_nav，**不给 GSZ**，
      估算净值由 direct.py 反推（base_nav × (1+chg/100)）。
    """

    name = "sina"
    BASE_URL = "https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/FdFundService.getEstimateNetworthPic"

    def fetch_valuation(self, codes: Sequence[str]) -> dict[str, DirectQuote]:
        """逐只查询。单只失败只记 debug 日志，不影响同批其它基金。"""
        result: dict[str, DirectQuote] = {}
        for code in codes:
            try:
                result[code] = self._fetch_one(code)
            except DataSourceError as exc:
                # 单只失败不影响同批其它基金。
                LOGGER.debug("新浪估值失败 %s: %s", code, exc)
        if not result:
            # 一只都没成功才算本源整体失败，触发上层换源。
            raise DataSourceError(self.name, "新浪估值接口未返回任何数据")
        return result

    def _fetch_one(self, code: str) -> DirectQuote:
        """取单只基金的估值曲线末点。"""
        try:
            payload = self._http.get_json(
                self.BASE_URL,
                params={"symbol": code, "callback": ""},
                headers={"Referer": "https://finance.sina.com.cn/"},
            )
        except Exception as exc:
            raise DataSourceError(self.name, f"{code} 估值请求失败: {exc}") from exc

        data = self._extract_data(payload)
        networth = data.get("networth") or []
        if not networth:
            # 空序列属于"该基金没有估值数据"的业务状态，用空 DirectQuote 表达。
            # 这里绝不能抛异常，否则主动基金会从 not_supported 变成 failed。
            return DirectQuote(fund_code=code, source=self.name)

        last = networth[-1]
        base_nav = safe_float(last.get("pre_nav"))
        raw_growth = safe_float(last.get("growthrate"))
        return DirectQuote(
            fund_code=code,
            fund_name=str(data.get("name") or ""),
            gszzl=_to_percent(raw_growth),
            gz_time=str(last.get("min_time") or "") or None,
            nav=base_nav,
            nav_date=str(last.get("pre_date") or "") or None,
            source=self.name,
        )

    @staticmethod
    def _extract_data(payload: object) -> dict:
        """剥掉 `{result: {data: {...}}}` 两层包裹，任一环节类型不对就返回空 dict。"""
        if not isinstance(payload, dict):
            return {}
        result = payload.get("result")
        if not isinstance(result, dict):
            return {}
        data = result.get("data")
        return data if isinstance(data, dict) else {}


def _to_percent(growth: float | None) -> float | None:
    """新浪 growthrate 为小数形式（0.0123 = 1.23%），部分响应已是百分数。

    以 0.3 为界：A 股基金单日涨跌幅超过 30% 极不寻常，一旦超过就认为该字段
    已经是百分数，直接采用，避免把 1.23% 放大成 123%。
    """
    if growth is None:
        return None
    return growth * 100.0 if abs(growth) <= 0.3 else growth
