# 基金实时估值系统（纯后端 · 数据只写 SQLite）

按 YAML 配置的基金列表，盘中定时抓取**直接估值**或**持仓 + 个股行情**，计算估值后写入
SQLite3。**没有前端页面、没有 Web 服务、没有报表渲染** —— 进程的唯一产出就是数据库里的行。

- 双轨估值：接口直接取值（`api`）优先，缺失时自动降级为持仓加权合成（`synthetic`）
- 多源备份：估值、持仓、行情三条链路各有主备数据源，单个接口失效不影响整批
- 可解释性：`source` / `confidence` / `coverage_pct` / `error_msg` 如实记录，下游可判断可信度
- 单只基金失败只落一条 `failed` 记录，不影响同批其它基金

---

## 1. 快速开始

使用 [uv](https://docs.astral.sh/uv/) 管理环境（已创建 `.venv`，Python 3.13）：

```bash
uv venv .venv --python 3.13          # 创建虚拟环境（已存在则跳过）
uv pip install -r requirements.txt   # 或 requirements-dev.txt（含 pytest / ruff）

# 单次计算（手动触发，不受交易时段限制）
uv run python -m fund_valuation.main --config fund_config.yml --once

# 常驻轮询（按 runtime.interval_seconds 循环，非交易时段自动休眠）
uv run python -m fund_valuation.main --config fund_config.yml
```

未安装 uv 时，用等效的原生方式：

```bash
python -m venv .venv && ./.venv/Scripts/activate   # Windows（Linux/macOS: source .venv/bin/activate）
pip install -r requirements.txt
python -m fund_valuation.main --config fund_config.yml --once
```

依赖：Python 3.13、`requests`、`PyYAML`、`beautifulsoup4`、`lxml`；数据库用标准库 `sqlite3`。

命令行参数：

| 参数 | 说明 |
|---|---|
| `--config PATH` | YAML 配置路径，默认 `fund_config.yml` |
| `--once` | 只执行一轮后退出；**忽略** `trade_time_only` |
| `--iterations N` | 常驻模式下最多轮询 N 轮（调试用） |
| `--force` | 非交易时段也执行 |
| `--show` | 结束时把最近记录打印到 stdout（仅排查用，不写文件） |

数据库文件由 `runtime.db_path` 决定，首次运行自动建表，无需手工执行 SQL。

---

## 2. 目录结构

```
fund_valuation/
├── main.py                 # 入口：装配依赖、解析参数
├── config.py               # YAML 加载与校验（dataclass 建模）
├── db.py                   # SQLite 建表、迁移、读写封装（SQL 只在此处出现）
├── models.py               # 领域模型与枚举
├── scheduler.py            # 交易时段判断 + 轮询循环
├── valuation/
│   ├── engine.py           # 双轨调度：direct / synthetic 选择与降级
│   ├── direct.py           # 直接取值路径
│   └── synthetic.py        # 持仓加权合成 + 覆盖率/置信度
├── datasource/
│   ├── base.py             # 抽象基类（Valuation/Holdings/Quote/Nav）+ DataSourceError
│   ├── registry.py         # 配置字符串 → 实现类的工厂
│   ├── eastmoney.py        # 东财持仓（移动端 JSON）
│   ├── tiantian.py         # 天天基金：批量估值 / 档案持仓 / 基准净值
│   ├── tencent.py          # 腾讯行情（主）
│   └── sina.py             # 新浪行情（备）+ 估值曲线（备）
└── utils/
    ├── http.py             # UA、超时、重试、编码、代码归一化
    ├── html.py             # 表头定位式表格解析、权重归一
    ├── calendar.py         # 交易日/交易时段、下一窗口计算
    ├── timeutil.py         # ISO8601 与宽松时间解析
    └── logger.py           # 统一日志
```

分层约束：数据源层不碰数据库与配置加载，估值计算是纯函数，因此加权逻辑可以脱离网络单测。

**注释约定**（全仓库一致，便于顺着调用链阅读）：

- 每个模块开头写明**职责边界**与**执行顺序**，模块之间靠这段文字串起来；
- 每个类/函数都有一句 docstring 说明"做什么"，关键处再补"**为什么这么做**"（如归一化、浮点容差、仓位折算的依据）；
- 分支代码注释解释**触发条件与后果**，而不是复述代码（例：`coverage <= 0` → 说明为什么这一支要降级到代理 ETF）；
- 测试用例的 docstring 直接写"这条断言在防止什么回归"。

按调用链阅读的顺序：`main` → `scheduler` → `valuation/engine` → `datasource/tiantian|eastmoney|tencent` → `valuation/synthetic`。


根目录文件：

| 文件 | 说明 |
|---|---|
| `fund_config.yml` | 待估值基金列表 + 运行时参数 |
| `README.md` | 本文档：安装、配置、算法、运维 |
| `PIPELINE.md` | **数据获取与计算逻辑详解**：调用链、请求参数、解析规则、降级矩阵 |
| `DATABASE.md` | **数据库逐字段说明**：取值、写入时机、NULL 语义、常用 SQL |
| `requirements.txt` / `requirements-dev.txt` | 运行依赖 / 含测试与 lint 的开发依赖 |
| `pytest.ini` | 测试配置 |

---

## 3. 双轨估值策略

| 路径 | 适用基金 | 数据 | `source` |
|---|---|---|---|
| **direct** | LOF、场内 ETF、部分场外指数基金 | 天天基金批量估值接口的 `GSZ` / `GSZZL` | `api` |
| **synthetic** | 主动权益、无直接估值的场外指数基金 | 季报前十大权重 × 个股实时涨跌，归一化 | `synthetic` |
| **fallback** | 无持仓且配置了 `fallback_etf` 的指数/联接基金 | 代理 ETF 实时涨跌 × 95% 仓位假设 | `fallback` |

`mode` 决定路径：

- `auto`（推荐）：先走 direct；接口返回空估值（主动基金被监管下线估值，属**合法状态**）或估值时间非当日 → 降级 synthetic；两条都失败才记 `failed`
- `direct`：只用直接取值，拿不到就是 `failed` / `not_supported`
- `synthetic`：只用合成路径，连 direct 请求都不发

---

## 4. 数据源与实测状态（2026-10 校验）

| 环节 | 主源 | 备源 | 备注 |
|---|---|---|---|
| 基金估值 | 天天基金 `FundValuationLast`（批量 ≤50/次） | 新浪 `getEstimateNetworthPic` | 响应外层为小写 `data`，已兼容 `Datas/data/Data` |
| 基金持仓 | 东财移动端 `FundMNInverstPosition` | 天天基金 `FundArchivesDatas`（HTML 片段） | 见下方失效说明 |
| 个股行情 | 腾讯 `qt.gtimg.cn`（A股/港股/美股/ETF） | 新浪 `hq.sinajs.cn`（仅 A 股/ETF） | 新浪需 GBK + Referer |
| 基准净值 | 天天基金 `pingzhongdata` | `f10/lsjz` | 两者都在 `TiantianNavSource` 内部串行尝试 |

已失效或受限的端点（不要再用）：

- `api.fund.eastmoney.com/f10/ccmx` → 返回 `ErrCode=4 / 404`
- `fundf10.eastmoney.com/ccmx_{code}.html` → 页面改为 JS 渲染，静态 HTML 内无持仓表
- 移动端 `FundMNInverstPosition` **必须**带移动端 UA + `Referer: https://h5.1234567.com.cn/`，否则返回 `ErrCode=61136403 网络繁忙`

因此"东方财富 F10 页面 HTML 解析"这条路径，实际落在了它背后的 `FundArchivesDatas` 数据接口上（由 `tiantian.py` 承担）；两条链路使用不同端点，互为备份。

行情/估值在必要时自动降级：单源异常只记录并切换下一源；某只股票无有效报价（停牌、返回 0）时跳过该股并从覆盖率中剔除。

---

## 5. 估值算法

> 本章讲算法本身。若需要**端到端执行链路**（每个阶段调了哪个接口、传了什么参数、
> 解析规则与降级分支），见 **[PIPELINE.md](./PIPELINE.md)**。

持仓加权合成：

```
total_weight        = Σ(成功取到行情的持仓权重)
weighted_change     = Σ(权重 × 个股涨跌幅)
estimated_change    = weighted_change / total_weight      ← 归一化
estimated_nav       = base_nav × (1 + estimated_change / 100)
coverage_pct        = total_weight
```

**为什么要除以 `total_weight`**：前十大重仓通常只占净值 30%–80%，若直接取 Σ(w×r)，
结果会被系统性低估（相当于假设剩余仓位不涨不跌）。归一化等价于"假设未覆盖部分与重仓股同向同幅"。

个股涨跌幅一律用 `(现价 - 昨收) / 昨收` 现算，不直接信任接口的涨跌幅字段（口径不一致时容易出错）。

置信度（`classify_confidence`）：

| 条件 | confidence |
|---|---|
| `coverage >= max_holdings_pct` | `high` |
| `coverage >= 0.75 × max_holdings_pct` | `medium` |
| 其余，或有效持仓数为 0 | `low` |
| `source=fallback`（代理 ETF） | 固定 `low` |

> 提醒：`max_holdings_pct` 是"期望覆盖率阈值"。主动基金前十大常只占 40%–60%，
> 若配成 0.80，绝大多数主动基金将长期为 `low`；希望中等覆盖也标 `medium` 时把它下调到 0.5 左右。

---

## 6. 配置说明（`fund_config.yml`）

`funds[]`：

| 字段 | 必填 | 说明 |
|---|---|---|
| `code` | 是 | 6 位基金代码 |
| `name` | 否 | 便于日志排查；也会写回 `funds.fund_name` |
| `mode` | 否 | `auto` / `direct` / `synthetic`，默认 `auto`（非法值直接报错） |
| `holdings_source` | 否 | `eastmoney` / `tiantian`，默认 `eastmoney`；另一个自动作为备份 |
| `max_holdings_pct` | 否 | (0,1] 小数，默认 0.80，用于置信度分档 |
| `fallback_etf` | 否 | 代理 ETF 代码（如 `sh518880`），无持仓时使用；无则填 `null` |

`runtime`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `interval_seconds` | 60 | 轮询间隔 |
| `trade_time_only` | true | 非交易时段跳过（`--once` 不受限） |
| `db_path` | ./fund_valuation.db | SQLite 路径 |
| `request_timeout` | 8 | 单次 HTTP 超时（秒） |
| `batch_size` | 50 | 批量估值单批上限，限制 1..50 |
| `user_agent` | 浏览器 UA | 请求东财/新浪/天天基金使用 |
| `log_level` | INFO | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `max_workers` | 4 | 合成路径并发线程数，1..32 |
| `validate_gztime` | true | 校验 `GZTIME` 是否为当日，防止隔夜旧值入库 |
| `retry_times` | 2 | 网络失败重试次数（仅对超时/连接错误/5xx 重试） |
| `holidays_file` | null | 自定义休市日文件，每行一个 `YYYY-MM-DD` |

校验策略是"宁可启动失败也不静默降级"：未知字段、枚举拼错、阈值越界都会抛 `ConfigError`
并指出 `funds[1].mode` 这类具体位置，进程以退出码 2 结束。

---

## 7. 数据库表结构

> 逐字段说明（含取值范围、写入时机、NULL 语义、易错点、常用 SQL）见 **[DATABASE.md](./DATABASE.md)**。

启用 `PRAGMA journal_mode=WAL`，支持读写并发。

```sql
funds(fund_code PK, fund_name, fund_type, last_holdings_update)
holdings_snapshot(id, fund_code, snapshot_time, asset_code, asset_name, weight, asset_type, report_date)
valuation(id, fund_code, ts, estimated_nav, estimated_change_pct, base_nav,
          coverage_pct, confidence, source, status, data_source, error_msg,
          UNIQUE(fund_code, ts))
```

- `weight`、`coverage_pct` 均为**小数形式**（0.085 = 8.5%），不是百分数
- `holdings_snapshot.report_date` 是在需求基础上**额外增加的列**，记录季报报告期
  （如 `2026-09-30`），用于判断持仓是否过期；旧库会自动 `ALTER TABLE` 补列
- `valuation` 写入用 `ON CONFLICT(fund_code, ts) DO UPDATE`，同秒重复计算不会产生重复行

`status` 语义：

| 值 | 含义 |
|---|---|
| `available` | 成功算出估值 |
| `not_supported` | 接口明确无实时估值（主动基金常态），**不是失败** |
| `failed` | 所有数据源均不可用，`estimated_nav` 为 NULL，`error_msg` 说明原因 |

`source` 语义：`api`（消费第三方估值结果）/ `synthetic`（自主加权合成）/ `fallback`（代理 ETF 近似）。

查询示例：

```bash
# 最近 20 条
sqlite3 fund_valuation.db "SELECT ts,fund_code,estimated_change_pct,coverage_pct,confidence,source,status
                           FROM valuation ORDER BY ts DESC LIMIT 20;"

# 当日各基金最新一条
sqlite3 fund_valuation.db "SELECT fund_code, MAX(ts), estimated_change_pct, source
                           FROM valuation WHERE ts LIKE '2026-10-27%' GROUP BY fund_code;"

# 健康度：近 24 小时 source/confidence 分布
sqlite3 fund_valuation.db "SELECT source, confidence, COUNT(*) FROM valuation
                           WHERE ts >= datetime('now','localtime','-1 day') GROUP BY 1,2;"
```

> 未安装 `sqlite3` 命令行时，可用 `python -m sqlite3 fund_valuation.db` 或任意 SQLite 客户端
> （DB Browser for SQLite / DBeaver）打开同一文件，表结构一致。

---

## 8. 运维

**常驻进程**（推荐 systemd / supervisor 托管）：

```bash
uv run python -m fund_valuation.main --config /opt/fund/fund_config.yml
```

**定时任务**（每 5 分钟一次，只在交易时段执行，其余时间直接退出）：

```cron
*/5 9-15 * * 1-5 cd /opt/fund && .venv/bin/python -m fund_valuation.main --once >> val.log 2>&1
```

> 生产环境建议用 `.venv/bin/python` 的绝对路径（不依赖 `uv` 是否在 PATH 中）。

日志每轮输出一条汇总：总数 / 成功 / 无估值 / 失败 / 耗时 / `source` 与 `confidence` 分布，
可作为健康指标；单只基金失败会同时写入日志与 `valuation.error_msg`。

数据量估算：60 秒间隔 × 4 小时 × 每只基金 ≈ 240 行/日/基金。长期运行建议定期归档：

```bash
sqlite3 fund_valuation.db "DELETE FROM valuation WHERE ts < date('now','-90 day'); VACUUM;"
```

---

## 9. 常见问题

**Q：为什么主动基金总是 `not_supported`？**
监管要求下，第三方平台已下线主动权益基金的盘中估值，接口 `GSZ/GSZZL` 返回空。`auto` 模式下会自动
转 synthetic 路径，`not_supported` 只出现在 `direct` 模式或合成也失败的情况下。

**Q：`failed` 和 `not_supported` 有什么区别？**
前者是"拿不到数据"（网络全挂、持仓与行情都无），后者是"数据源明确告知没有这项数据"。下游告警应
只对 `failed` 敏感。

**Q：合成估值能当净值用吗？**
不能。它只基于季报前十大重仓（披露滞后 15 天以上，且基金经理可能已调仓），误差客观存在，
只能作为**方向性参考**。`coverage_pct` 与 `confidence` 是判断可信度的前提，不要忽略。

**Q：为什么 `confidence` 一直是 `low`？**
见第 5 节提醒：`max_holdings_pct` 配得过高。把主动基金的阈值下调到接近其历史平均覆盖率即可。

**Q：某个接口改版了怎么办？**
改对应的 `datasource/*.py` 或在 `registry.py` 加分支，其它层不需要动。若只是响应字段变化，
优先在对应数据源内部加兼容分支，并在 `tests/` 补一个解析用例。

**Q：需要前端看数据？**
本系统刻意不含 UI。直接读 SQLite，或另建独立的可视化工程消费该库。

---

## 10. 测试

```bash
uv pip install -r requirements-dev.txt
uv run python -m pytest          # 68 项，全部离线（无网络依赖）
uv run python -m ruff check fund_valuation tests --select E,F,W --ignore E501
```

覆盖重点：加权归一化（含缺失行情剔除、无效报价、零覆盖）、置信度分档边界、
YAML 校验（非法枚举/未知字段/越界值/重复代码）、双轨降级、代码归一化、表格解析、交易日历。

---

## 11. 免责声明

- 数据来自东方财富/天天基金/腾讯/新浪的公开接口，不保证实时性与准确性，接口可能随时变更或限流
- direct 路径对 LOF/ETF/部分指数基金是权威估值；synthetic 路径是**方向性近似**，不构成投资依据
- 仅供学习研究，请勿用于商业用途

---

## 12. 与参考项目的关系

数据获取方式与估值思路参考了开源项目 [hzm0321/real-time-fund](https://github.com/hzm0321/real-time-fund)：

- 批量估值接口与字段裁剪（`FCODES` + `FIELDS`）、`GSZ = NAV × (1 + GSZZL/100)` 的换算
- 腾讯行情按 `~` 分段解析、移动端持仓接口的参数形态、归一化加权的数学形式
- ETF 代理的 95% 仓位假设

差异：参考项目是 Next.js + Supabase 的前端应用，本项目是纯后端进程，数据只落本地 SQLite，
并补充了官方接口失效后的替代路径（F10 页面 → 档案数据接口）、`GZTIME` 隔夜校验与置信度分级。
