"""YAML 配置校验测试：非法输入必须报错，且错误信息可定位。

测试组织方式是"同一个合法模板 + 局部替换"，因此每个用例只需关注
自己那一个字段的破坏方式，失败时也能立刻看出是哪个校验点失守。
"""

from __future__ import annotations

import pytest

from fund_valuation.config import ConfigError, load_config
from fund_valuation.models import FundMode, HoldingsSource

# 可用的最小完整配置：一只 auto（含全部字段）+ 一只 direct（只写必要字段）。
VALID = """
funds:
  - code: "005827"
    name: "易方达蓝筹精选混合"
    mode: "auto"
    holdings_source: "eastmoney"
    max_holdings_pct: 0.80
    fallback_etf: null
  - code: "161725"
    mode: "direct"
runtime:
  interval_seconds: 30
  db_path: ":memory:"
"""


def _write(tmp_path, content: str):
    """把配置写到临时目录，避免测试之间互相污染。"""
    path = tmp_path / "fund_config.yml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_valid_config(tmp_path):
    """合法配置应完整还原字段，未写的 runtime 项取默认值。"""
    config = load_config(_write(tmp_path, VALID))

    assert len(config.funds) == 2
    first = config.funds[0]
    assert first.code == "005827"
    assert first.mode is FundMode.AUTO
    assert first.holdings_source is HoldingsSource.EASTMONEY
    assert first.max_holdings_pct == pytest.approx(0.80)
    assert first.fallback_etf is None
    assert config.runtime.interval_seconds == 30
    # 缺省字段使用默认值
    assert config.runtime.batch_size == 50
    assert config.runtime.max_workers == 4


def test_missing_file(tmp_path):
    """配置文件不存在时给出明确提示，而不是抛 FileNotFoundError。"""
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.yml")


def test_empty_funds(tmp_path):
    """基金列表为空视为配置错误：没有基金可估值，进程无意义。"""
    with pytest.raises(ConfigError, match="funds"):
        load_config(_write(tmp_path, "funds: []\n"))


def test_invalid_mode_reports_allowed_values(tmp_path):
    """枚举值写错时，错误信息要同时给出位置与全部合法取值。"""
    content = VALID.replace('mode: "direct"', 'mode: "realtime"')
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, content))
    message = str(excinfo.value)
    # 定位到第二个基金（索引 1）的 mode 字段。
    assert "funds[1].mode" in message
    assert "auto" in message and "synthetic" in message


def test_invalid_holdings_source(tmp_path):
    """持仓源同样走枚举校验，非法值不会被静默降级为默认值。"""
    content = VALID.replace('holdings_source: "eastmoney"', 'holdings_source: "xueqiu"')
    with pytest.raises(ConfigError, match="holdings_source"):
        load_config(_write(tmp_path, content))


def test_max_holdings_pct_out_of_range(tmp_path):
    """写成 80（百分数）而非 0.80 必须报错 —— 这是最典型的误配。"""
    content = VALID.replace("max_holdings_pct: 0.80", "max_holdings_pct: 80")
    with pytest.raises(ConfigError, match="max_holdings_pct"):
        load_config(_write(tmp_path, content))


def test_invalid_code_format(tmp_path):
    """基金代码必须是 6 位数字，长度不符直接拒绝。"""
    content = VALID.replace('code: "005827"', 'code: "5827"')
    with pytest.raises(ConfigError, match="6 位数字"):
        load_config(_write(tmp_path, content))


def test_duplicate_codes_rejected(tmp_path):
    """重复代码会导致同轮结果互相覆盖，必须在加载阶段拦住。"""
    content = VALID.replace('code: "161725"', 'code: "005827"')
    with pytest.raises(ConfigError, match="重复"):
        load_config(_write(tmp_path, content))


def test_unknown_field_rejected(tmp_path):
    """字段名拼错（max_holding_pct 少个 s）必须报错，否则会静默用默认阈值。"""
    content = VALID.replace('mode: "direct"', 'mode: "direct"\n    max_holding_pct: 0.5')
    with pytest.raises(ConfigError, match="未知字段"):
        load_config(_write(tmp_path, content))


def test_batch_size_upper_bound(tmp_path):
    """批量接口上限 50，超过会被截断，因此配置层硬性限制。"""
    content = VALID + "  batch_size: 80\n"
    with pytest.raises(ConfigError, match="batch_size"):
        load_config(_write(tmp_path, content))


def test_invalid_log_level(tmp_path):
    """日志级别只接受标准库的五个取值。"""
    content = VALID + "  log_level: TRACE\n"
    with pytest.raises(ConfigError, match="log_level"):
        load_config(_write(tmp_path, content))


def test_shipped_config_is_valid():
    """仓库自带示例配置必须通过校验，避免文档与代码脱节。"""
    config = load_config("fund_config.yml")
    assert len(config.funds) >= 2
    # auto / direct / synthetic 三者都是合法取值，非法值在 load_config 阶段就已抛错。
    # 这里额外要求示例覆盖全部三条路径，防止示例退化成单一场景、文档失去参考价值。
    modes = {f.mode for f in config.funds}
    assert {FundMode.AUTO, FundMode.DIRECT, FundMode.SYNTHETIC} <= modes
