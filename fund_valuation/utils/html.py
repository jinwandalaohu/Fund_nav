"""HTML 表格解析工具（东方财富 F10 / 天天基金档案页共用）。

两个数据源返回的持仓表格结构相近但列顺序会变，因此按表头文本定位列，
而不是写死列下标 —— 这样页面改版时只需保证表头关键字不变即可继续工作。

同时提供两个跨模块共用的小工具：
- normalize_weight      统一权重口径（百分数 / 小数混杂）
- extract_report_date   从页面文本提取季报报告期
"""

from __future__ import annotations

import calendar as _calendar
import re
from datetime import date

from bs4 import BeautifulSoup

from ..models import AssetType, Holding
from .http import safe_float
from .logger import get_logger

LOGGER = get_logger(__name__)

# 表头关键字候选：不同页面用词不同（股票代码/证券代码、占净值比例/占净值比率），
# 只要能匹配上任一候选即可定位列。顺序不重要，因为匹配是"包含"判断。
_CODE_HEADERS = ("股票代码", "债券代码", "证券代码", "代码")
_NAME_HEADERS = ("股票名称", "债券名称", "证券名称", "名称")
_WEIGHT_HEADERS = ("占净值比例", "占净值比率", "市值占净值比")

# 报告期三种常见写法："报告期：2025-06-30"、"2025-06-30"、"2025年2季度"。
_REPORT_DATE_PATTERN = re.compile(r"报告期[：:\s]*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
_ISO_DATE_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_QUARTER_PATTERN = re.compile(r"(\d{4})\s*年\s*第?\s*([一二三四1-4])\s*季度?")

_QUARTER_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _parse_soup(html: str) -> BeautifulSoup:
    """构造解析器。优先 lxml（快且容错好），未安装时退回标准库解析器。"""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml 未安装或解析异常时退回标准库
        return BeautifulSoup(html, "html.parser")


def _find_column(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    """在表头列表中找出第一个匹配候选关键字的列下标。

    先把表头里的空格与换行去掉，因为部分页面的表头是
    "占净值\n比例" 这种带换行的多行文本。
    """
    for index, header in enumerate(headers):
        cleaned = header.replace(" ", "").replace("\n", "")
        if any(candidate in cleaned for candidate in candidates):
            return index
    return None


def _header_cells(row) -> list[str]:
    """取一行里所有单元格的文本。表头行可能是 th 也可能是 td，两者都要收。"""
    return [cell.get_text(strip=True) for cell in row.find_all(["th", "td"])]


def normalize_weight(value: float | str | None) -> float:
    """把权重统一为小数形式。

    接口口径不统一：东财移动端 JZBL 是 "9.53"（百分比），F10 表格是 "9.53%"，
    部分源直接给 0.0953。大于 1 一律按百分数处理，否则原样返回。

    选 1.0 作为分界而非 0.5：单只股票占基金净值超过 100% 不可能，
    因此 >1 必然是百分数；而 0.53 这种值若按百分数理解只有 0.0053，明显不合理。
    """
    number = safe_float(value)
    if number is None or number <= 0:
        # 缺失或负数权重无法参与加权，返回 0 由调用方过滤。
        return 0.0
    return number / 100.0 if number > 1.0 else number


def parse_holdings_table(html: str) -> tuple[list[Holding], str | None]:
    """从持仓页面/片段中解析前十大重仓股，返回 (持仓列表, 报告期)。

    页面可能含多张表（资产配置表、行业配置表、持仓明细表），
    因此遍历所有 table，只保留"同时能定位到代码列与权重列"的那张，
    并在多张候选表中取行数最多的（明细表通常最完整）。
    """
    soup = _parse_soup(html)
    best: list[Holding] = []

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        headers = _header_cells(header_row)
        code_index = _find_column(headers, _CODE_HEADERS)
        weight_index = _find_column(headers, _WEIGHT_HEADERS)
        if code_index is None or weight_index is None:
            # 缺任一关键列就不是持仓明细表，跳过。
            continue
        # 名称列可有可无，只影响展示，不影响加权。
        name_index = _find_column(headers, _NAME_HEADERS)

        holdings: list[Holding] = []
        # [1:] 跳过表头行。
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) <= max(code_index, weight_index):
                # 列数不足说明是合并单元格/小标题行，跳过。
                continue
            code = cells[code_index].get_text(strip=True)
            # 代码可能是 600519 / 00700 / AAPL / 019547.SH，限定字符集与长度过滤噪声。
            if not re.fullmatch(r"[0-9A-Za-z.]{2,12}", code):
                continue
            weight = normalize_weight(cells[weight_index].get_text(strip=True))
            if weight <= 0:
                continue
            name = cells[name_index].get_text(strip=True) if name_index is not None else ""
            holdings.append(
                Holding(asset_code=code, asset_name=name, weight=weight, asset_type=AssetType.STOCK)
            )

        if len(holdings) > len(best):
            best = holdings

    # 按权重降序，保证"前十大"的语义；再截断 10 条。
    best.sort(key=lambda h: h.weight, reverse=True)
    return best[:10], extract_report_date(soup.get_text(" ", strip=True))


def extract_report_date(text: str) -> str | None:
    """从页面文本中提取报告期，统一输出 YYYY-MM-DD。

    季报的披露日期格式五花八门（"报告期：2025-06-30"、"2025年2季度"），
    统一折算为季度末日期，便于下游判断数据新鲜度。

    三级兜底顺序：带"报告期"前缀的 → 任意 ISO 日期 → "YYYY年N季度"。
    前两者直接可用；季度写法需要换算成该季度最后一天（如 2 季度 → 06-30）。
    """
    match = _REPORT_DATE_PATTERN.search(text)
    if match:
        year, month, day = (int(x) for x in match.groups())
        return _safe_date(year, month, day)

    match = _ISO_DATE_PATTERN.search(text)
    if match:
        year, month, day = (int(x) for x in match.groups())
        return _safe_date(year, month, day)

    match = _QUARTER_PATTERN.search(text)
    if match:
        year = int(match.group(1))
        quarter = _QUARTER_MAP.get(match.group(2))
        if quarter:
            month = quarter * 3
            # monthrange 返回 (星期几, 当月天数)，取后者得到季度末。
            last_day = _calendar.monthrange(year, month)[1]
            return date(year, month, last_day).isoformat()
    return None


def _safe_date(year: int, month: int, day: int) -> str | None:
    """构造日期，非法组合（如 2025-02-30）返回 None 而不是抛异常。"""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def guess_fund_type(name: str | None, asset_types: set[AssetType] | None = None) -> str:
    """根据基金名称推断类型，仅用于 funds.fund_type 的粗分类展示。

    名称是唯一稳定的线索（持仓类型要到首次抓取后才知道），因此优先级：
    ETF/联接 > LOF > 指数 > 债券货币 > 股票 > 混合 > unknown。

    判断顺序即优先级：必须先判"联接"，否则 "XXETF联接C" 会被当成场内 ETF。
    """
    label = (name or "").upper()
    if "联接" in label:
        # 联接基金本质是场外基金，但跟踪标的是 ETF，归到 etf 更贴近使用习惯。
        return "etf"
    if "ETF" in label:
        return "etf"
    if "LOF" in label:
        return "lof"
    if "指数" in label or "ETF" in label:
        return "index"
    if "债" in label or "货币" in label:
        # 债券与货币基金都不适用股票加权，统一归到 bond 提醒使用方注意。
        return "bond"
    if "股票" in label:
        return "equity"
    if "混合" in label:
        return "hybrid"
    if asset_types and AssetType.STOCK in asset_types:
        # 名称无线索时退化为"持仓里有没有股票"。
        return "equity"
    return "unknown"
