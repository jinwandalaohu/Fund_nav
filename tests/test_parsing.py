"""解析与工具函数测试：代码归一化、权重口径、F10 表格解析。"""

from __future__ import annotations

import pytest

from fund_valuation.datasource.eastmoney import EastmoneyHoldingsSource
from fund_valuation.datasource.tiantian import TiantianHoldingsSource
from fund_valuation.models import WeightUnit
from fund_valuation.utils.html import normalize_weight, parse_holdings_table
from fund_valuation.utils.http import normalize_code, safe_float, split_code
from fund_valuation.utils.timeutil import parse_datetime, same_day

# 精简版 F10 持仓页：与真实页面结构一致（报告期在表格外的标题里，权重带 %）。
F10_HTML = """
<html><body>
<div class="box"><h4 class="t">报告期：2026-06-30</h4>
<table class="w782 comm tzxq">
  <thead><tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>占净值比例</th><th>持股数</th></tr></thead>
  <tbody>
    <tr><td>1</td><td><a href="#">600519</a></td><td>贵州茅台</td><td>9.53%</td><td>120.00</td></tr>
    <tr><td>2</td><td><a href="#">000858</a></td><td>五粮液</td><td>8.10%</td><td>300.00</td></tr>
    <tr><td>3</td><td><a href="#">00700</a></td><td>腾讯控股</td><td>5.00%</td><td>50.00</td></tr>
  </tbody>
</table></div>
</body></html>
"""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "sh600519"),   # 沪市主板
        ("000001", "sz000001"),   # 深市主板
        ("300750", "sz300750"),   # 创业板
        ("430047", "bj430047"),   # 北交所
        ("00700", "hk00700"),     # 5 位港股
        ("AAPL", "usAAPL"),       # 美股
        ("600519.SH", "sh600519"),  # 后缀式
        ("700.HK", "hk00700"),      # 港股后缀式需补齐 5 位
        ("sh600519", "sh600519"),   # 已带前缀
        ("SZ000001", "sz000001"),   # 前缀大小写不敏感
    ],
)
def test_normalize_code(raw, expected):
    """归一化是"持仓代码 ↔ 行情键"的桥，任一规则失效都会导致行情全部查不到。"""
    assert normalize_code(raw) == expected


def test_split_code():
    """拆分市场与数字部分，供需要分市场的接口使用。"""
    assert split_code("600519") == ("sh", "600519")
    assert split_code("hk00700") == ("hk", "00700")


def test_safe_float_keeps_empty_vs_zero_distinction():
    """GSZ='' 与 GSZ='0' 语义不同，必须区分 None 与 0.0。"""
    assert safe_float("") is None
    assert safe_float(None) is None
    assert safe_float("--") is None
    assert safe_float(0) == 0.0
    assert safe_float("0") == 0.0
    assert safe_float("1.23%") == pytest.approx(1.23)


@pytest.mark.parametrize(
    ("raw", "unit", "expected"),
    [
        ("9.53%", WeightUnit.PERCENT, 0.0953),
        (9.53, WeightUnit.PERCENT, 0.0953),
        ("95.3", WeightUnit.PERCENT, 0.953),
        # 回归用例：联接基金的残余持仓 "0.52" 是 0.52%，绝不能被当成 52%。
        # 旧实现按 ">1 才除以 100" 推断，这里会得到 0.52，进而把 coverage 算成 253%。
        ("0.52", WeightUnit.PERCENT, 0.0052),
        (0.52, WeightUnit.PERCENT, 0.0052),
        # 小数口径原样保留，不做任何推断（9.53 就是 9.53，不会被"修正"成 0.0953）。
        (0.0953, WeightUnit.DECIMAL, 0.0953),
        (9.53, WeightUnit.DECIMAL, 9.53),
        ("", WeightUnit.PERCENT, 0.0),
        ("-1", WeightUnit.PERCENT, 0.0),
        (None, WeightUnit.DECIMAL, 0.0),
    ],
)
def test_normalize_weight_respects_declared_unit(raw, unit, expected):
    """单位由数据源声明，不靠数值大小推断；空值与负值一律归 0。"""
    assert normalize_weight(raw, unit) == pytest.approx(expected)


def test_parse_holdings_table():
    """按表头定位列，能正确取到代码/名称/权重与报告期。"""
    holdings, report_date = parse_holdings_table(F10_HTML)

    assert report_date == "2026-06-30"
    assert [h.asset_code for h in holdings] == ["600519", "000858", "00700"]
    assert holdings[0].asset_name == "贵州茅台"
    # "9.53%" 必须被归一为 0.0953，而不是 9.53。
    assert holdings[0].weight == pytest.approx(0.0953)


def test_parse_holdings_table_decimal_unit_keeps_values():
    """声明为小数口径时，表格里的 0.0953 原样保留，不再除以 100。"""
    html = (
        "<table><tr><th>股票代码</th><th>股票名称</th><th>占净值比例</th></tr>"
        "<tr><td>600519</td><td>贵州茅台</td><td>0.0953</td></tr></table>"
    )
    holdings, _ = parse_holdings_table(html, WeightUnit.DECIMAL)
    assert holdings[0].weight == pytest.approx(0.0953, rel=1e-6)


def test_parse_holdings_table_without_weights_returns_empty():
    """缺少"占净值比例"列的表不是持仓明细表，应返回空列表而不是乱解析。"""
    html = "<table><tr><th>股票代码</th><th>股票名称</th></tr><tr><td>600519</td><td>贵州茅台</td></tr></table>"
    assert parse_holdings_table(html)[0] == []


@pytest.mark.parametrize("source_cls", [EastmoneyHoldingsSource, TiantianHoldingsSource])
def test_holdings_source_declares_weight_unit(source_cls):
    """每个持仓源都必须自己声明口径。

    断言"类自身定义"而非"能取到值"：靠基类默认值继承会掩盖"新源忘了核对口径"，
    而口径错了会静默产出 100 倍偏差的权重。
    """
    assert "weight_unit" in vars(source_cls)
    assert isinstance(source_cls.weight_unit, WeightUnit)


def test_parse_datetime_formats():
    """接口时间的三种常见格式都要能解析，非法字符串返回 None。"""
    assert parse_datetime("2026-10-27 10:29") is not None
    assert parse_datetime("2026-10-27 10:29:30") is not None
    assert parse_datetime("garbage") is None


def test_same_day():
    """隔夜判定：同日为 True，跨日为 False，时间不可解析也为 False。"""
    from datetime import date

    assert same_day("2026-10-27 10:29", date(2026, 10, 27)) is True
    assert same_day("2026-10-24 15:00", date(2026, 10, 27)) is False
    assert same_day(None, date(2026, 10, 27)) is False
