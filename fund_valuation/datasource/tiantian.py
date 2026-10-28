"""天天基金数据源：批量直接估值（主）+ 持仓备用 + 基准净值。

三个角色分别独立成类，便于按需组合：
- TiantianValuationSource → 直接取值路径（同花顺口径的盘中估值 GSZ/GSZZL）
- TiantianHoldingsSource  → 持仓备用源（基金档案接口）
- TiantianNavSource       → 基准净值（pingzhongdata 优先，lsjz 兜底）

三者共用一套域名与 Referer 约定，因此文件级的常量集中放在顶部。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..models import DirectQuote, HoldingsSnapshot, NavPoint, WeightUnit
from ..utils.html import parse_holdings_table
from ..utils.http import safe_float
from ..utils.logger import get_logger
from .base import DataSourceError, HoldingsSource, NavSource, ValuationSource

LOGGER = get_logger(__name__)

# 只请求需要的字段：字段越多响应越慢，且接口对字段名拼写错误会静默返回空。
_FIELDS = "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE"
# 档案接口返回 JS 变量包裹的 HTML 片段：var apidata={ content:"<table>...",arryear:[...] }
_API_DATA_PATTERN = re.compile(r'content\s*:\s*"(?P<content>.*?)"\s*,\s*arryear', re.DOTALL)
# pingzhongdata 里的净值序列：var Data_netWorthTrend = [{"x":毫秒,"y":净值,"equityReturn":...},...];
_NET_WORTH_PATTERN = re.compile(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", re.DOTALL)
_FUND_NAME_PATTERN = re.compile(r'var\s+fS_name\s*=\s*"([^"]*)"')


class TiantianValuationSource(ValuationSource):
    """批量估值接口。

    GSZ/GSZZL 为空是主动权益基金的常态（监管下线估值），属于合法业务状态：
    本类只如实返回空 DirectQuote，绝不抛异常，由引擎决定是否降级到合成路径。
    """

    name = "tiantian"
    BASE_URL = "https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast"
    HEADERS = {"Referer": "https://fund.eastmoney.com/"}

    def fetch_valuation(self, codes: Sequence[str]) -> dict[str, DirectQuote]:
        """一次请求覆盖一批基金（FCODES 逗号拼接），这是直接路径高效的关键。"""
        if not codes:
            return {}
        try:
            payload = self._http.get_json(
                self.BASE_URL,
                params={"FCODES": ",".join(codes), "FIELDS": _FIELDS},
                headers=self.HEADERS,
            )
        except Exception as exc:
            raise DataSourceError(self.name, f"批量估值请求失败: {exc}") from exc

        rows = _extract_rows(payload)
        if rows is None:
            # 结构异常（如接口改成 HTML 报错页）必须报错，否则会被误判成"全部无估值"。
            raise DataSourceError(self.name, "批量估值响应结构异常，未找到 Datas 列表")

        result: dict[str, DirectQuote] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            code = str(item.get("FCODE") or "").strip()
            if not code:
                continue
            # 注意：这里不判断 GSZ 是否为空，空值也要返回（见类文档）。
            result[code] = DirectQuote(
                fund_code=code,
                fund_name=str(item.get("SHORTNAME") or "").strip(),
                gsz=safe_float(item.get("GSZ")),
                gszzl=safe_float(item.get("GSZZL")),
                gz_time=_normalize_gztime(item.get("GZTIME")),
                # NAV 是前一交易日单位净值，作为 base_nav。
                nav=safe_float(item.get("NAV")),
                nav_date=str(item.get("PDATE") or "").strip() or None,
                source=self.name,
            )
        return result


class TiantianHoldingsSource(HoldingsSource):
    """持仓备用源：基金档案接口 FundArchivesDatas（返回 JS 变量包裹的 HTML 片段）。

    这是"东财 F10 持仓页"背后的真实数据接口：页面本身已改成 JS 渲染，
    静态抓 HTML 拿不到表格，但该接口返回的 content 里就是服务器渲染好的 <table>。
    """

    name = "tiantian"
    ARCHIVES_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    HEADERS = {"Referer": "https://fundf10.eastmoney.com/"}
    # 档案接口的 "占净值比例" 列带 % 后缀，属百分数口径；
    # 显式声明后由 parse_holdings_table 透传，不再按数值大小猜测。
    weight_unit = WeightUnit.PERCENT

    def fetch_holdings(self, fund_code: str) -> HoldingsSnapshot:
        """取前十大重仓股 HTML 片段并解析。"""
        params = {
            "type": "jjcc",  # jjcc = 基金持仓
            "code": fund_code,
            "topline": "10",  # 只要前十大
            "year": "",
            "month": "",
            # 时间戳随机参数用于绕过 CDN 缓存，否则可能拿到上一季度的旧数据。
            "rt": f"{datetime.now().timestamp():.6f}",
        }
        try:
            text = self._http.get_text(self.ARCHIVES_URL, params=params, headers=self.HEADERS)
        except Exception as exc:
            raise DataSourceError(self.name, f"{fund_code} 档案接口请求失败: {exc}") from exc

        content = _extract_archive_content(text)
        if not content:
            raise DataSourceError(self.name, f"{fund_code} 档案接口未返回持仓内容")

        # 解析与权重口径归一都复用 utils.html，和东财/其它页面保持同一套逻辑；
        # weight_unit 透传下去，保证"单位"仍由本源声明而非解析器猜。
        holdings, report_date = parse_holdings_table(content, self.weight_unit)
        holdings = self._require_holdings(fund_code, holdings, self.name)
        return HoldingsSnapshot(
            fund_code=fund_code,
            holdings=tuple(holdings[:10]),
            snapshot_time=datetime.now().replace(microsecond=0).isoformat(),
            report_date=report_date,
            source=f"{self.name}:archives",
        )


class TiantianNavSource(NavSource):
    """基准净值源。

    优先 pingzhongdata：一次请求同时给出全部历史净值与基金名称，无频率限制问题；
    失败时退化到 lsjz 接口（单页两条，够用）。
    """

    name = "tiantian"
    PINGZHONGDATA = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
    LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
    HEADERS = {"Referer": "https://fundf10.eastmoney.com/"}

    def fetch_nav(self, fund_code: str) -> NavPoint:
        """双策略取基准净值，任一成功即返回，全失败才抛异常。"""
        errors: list[str] = []
        try:
            return self._from_pingzhongdata(fund_code)
        except DataSourceError as exc:
            errors.append(exc.message)
            LOGGER.debug("pingzhongdata 净值失败 %s: %s", fund_code, exc)

        try:
            return self._from_lsjz(fund_code)
        except DataSourceError as exc:
            errors.append(exc.message)

        # 合并两段原因，便于判断是"接口变了"还是"基金没有净值数据"。
        raise DataSourceError(self.name, f"{fund_code} 基准净值获取失败: " + " | ".join(errors))

    def fetch_name(self, fund_code: str) -> str | None:
        """尽力获取基金简称（失败返回 None，不影响估值流程）。

        与 fetch_nav 的区别：名称只用于展示，因此吞掉所有异常返回 None；
        净值缺失则必须失败，故两者对错误的处理策略不同。
        """
        try:
            text = self._http.get_text(
                self.PINGZHONGDATA.format(code=fund_code),
                params={"v": int(datetime.now().timestamp() * 1000)},
            )
        except Exception:
            return None
        match = _FUND_NAME_PATTERN.search(text)
        return match.group(1).strip() if match else None

    def _from_pingzhongdata(self, fund_code: str) -> NavPoint:
        """从 pingzhongdata 的 Data_netWorthTrend 序列取最后一个有效净值。"""
        try:
            text = self._http.get_text(
                self.PINGZHONGDATA.format(code=fund_code),
                params={"v": int(datetime.now().timestamp() * 1000)},
            )
        except Exception as exc:
            raise DataSourceError(self.name, f"{fund_code} pingzhongdata 请求失败: {exc}") from exc

        match = _NET_WORTH_PATTERN.search(text)
        if not match:
            raise DataSourceError(self.name, f"{fund_code} pingzhongdata 中无净值序列")

        try:
            series = json.loads(match.group(1))
        except ValueError as exc:
            raise DataSourceError(self.name, f"{fund_code} 净值序列解析失败: {exc}") from exc

        # 从末尾往前找：最后一点可能 y 为 null（当日净值未出），需要回退到前一交易日。
        for point in reversed(series):
            if not isinstance(point, dict):
                continue
            nav = safe_float(point.get("y"))
            if nav is not None and nav > 0:
                return NavPoint(nav=nav, date=_ms_to_date(point.get("x")))
        raise DataSourceError(self.name, f"{fund_code} 净值序列无有效数值")

    def _from_lsjz(self, fund_code: str) -> NavPoint:
        """从 lsjz（历史净值）接口取最近一条。pageSize=2 是为了拿到"今天 + 昨天"。"""
        params = {"fundCode": fund_code, "pageIndex": "1", "pageSize": "2"}
        try:
            payload = self._http.get_json(self.LSJZ_URL, params=params, headers=self.HEADERS)
        except Exception as exc:
            raise DataSourceError(self.name, f"{fund_code} lsjz 请求失败: {exc}") from exc

        if not isinstance(payload, dict):
            raise DataSourceError(self.name, f"{fund_code} lsjz 响应格式异常")
        data = payload.get("Data") or {}
        rows = data.get("LSJZList") if isinstance(data, dict) else None
        if not rows:
            raise DataSourceError(self.name, f"{fund_code} lsjz 无净值记录")

        for row in rows:
            # DWJZ = 单位净值，FSRQ = 净值日期。
            nav = safe_float(row.get("DWJZ"))
            if nav is not None and nav > 0:
                return NavPoint(nav=nav, date=str(row.get("FSRQ") or "").strip() or None)
        raise DataSourceError(self.name, f"{fund_code} lsjz 净值字段为空")


def _extract_rows(payload: Any) -> list[dict] | None:
    """兼容不同的响应包裹层。

    实测该接口返回小写 `data`，而移动端部分接口返回 `Datas`，
    因此对常见的大小写写法逐一尝试，避免接口微调就整体失效。
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return None

    for key in ("Datas", "datas", "data", "Data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            # 有些接口是 {Data: {Datas: [...]}} 的嵌套结构，再往里找一层。
            for inner_key in ("Datas", "datas", "data"):
                inner = value.get(inner_key)
                if isinstance(inner, list):
                    return [row for row in inner if isinstance(row, dict)]
    return None


def _normalize_gztime(value: Any) -> str | None:
    """GZTIME 可能是 '2026-10-27 15:00' 或 '2026-10-27 15:00:00'，统一去秒。

    只在确实带秒时才裁剪，否则 '15:00' 会被误剪成 '15'。
    """
    text = str(value or "").strip()
    if not text:
        return None
    # 前置断言 (?<=\d{2}:\d{2}) 保证只裁掉"第三个冒号之后"的秒部分。
    return re.sub(r"(?<=\d{2}:\d{2}):\d{2}$", "", text)


def _extract_archive_content(text: str) -> str | None:
    """从 `var apidata={ content:"<table>...</table>",arryear:[...] }` 中取出 HTML 片段。

    片段是 JS 字符串字面量，内部引号与斜杠都被转义，需要逐项还原后才能交给 HTML 解析器。
    """
    match = _API_DATA_PATTERN.search(text)
    if not match:
        return None
    content = match.group("content")
    return (
        content.replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\r", "")
        .replace("\\n", "")
        .replace("\\t", "")
    )


def _ms_to_date(value: Any) -> str | None:
    """毫秒时间戳转 YYYY-MM-DD。净值日期按北京时间对齐，故加 8 小时。"""
    millis = safe_float(value)
    if millis is None:
        return None
    try:
        # 直接加 8 小时而非用 timezone：接口给的是 UTC 毫秒，A 股净值必须落北京时间。
        return (datetime(1970, 1, 1) + timedelta(milliseconds=millis, hours=8)).date().isoformat()
    except (OverflowError, ValueError):
        return None
