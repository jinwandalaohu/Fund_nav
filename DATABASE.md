# 数据库字段说明

本文档逐字段解释 `fund_valuation.db` 中三张表的含义、取值范围与写入时机。
所有描述均与 `fund_valuation/db.py` 的 `SCHEMA` 一致，示例取自真实运行数据。

> 这些字段是**怎么算出来**的，见 [PIPELINE.md](./PIPELINE.md)：调用链、请求参数、解析规则、降级矩阵。

- 文件位置：`runtime.db_path`（默认 `./fund_valuation.db`）
- 引擎：Python 标准库 `sqlite3`，**无 ORM**
- 建表：首次运行由 `Database.initialize()` 自动执行 `CREATE TABLE IF NOT EXISTS`，无需手工 SQL
- 读写并发：`journal_mode=WAL`，允许"多读一写"

---

## 1. 连接参数

`Database._configure()` 在每次建立连接时执行：

| PRAGMA | 取值 | 作用 |
|---|---|---|
| `journal_mode` | `WAL` | 写不阻塞读，轮询期间可直接查库 |
| `synchronous` | `NORMAL` | WAL 下的推荐值，兼顾安全与写入速度 |
| `busy_timeout` | `5000` | 锁冲突最多等 5 秒，避免瞬时 `database is locked` |
| `foreign_keys` | `ON` | 已开启，但**当前三张表未定义外键约束**，故暂无实际效果（为将来扩展预留） |

连接参数：`check_same_thread=False` + 内部 `RLock`，因此估值可并发计算，但落库串行执行。

---

## 2. 三个必须先记住的约定

### 2.1 单位不统一（最容易出错）

| 字段 | 存的形态 | 实际含义 | 显示时 |
|---|---|---|---|
| `holdings_snapshot.weight` | 小数 | `0.0637` = 6.37% | × 100 |
| `valuation.coverage_pct` | **小数** | `0.4262` = 42.62% | × 100 |
| `valuation.estimated_change_pct` | **百分数** | `-0.32` = -0.32% | 直接显示 |

> ⚠️ `coverage_pct` 名字里带 `_pct`，但存的是**小数**（因为它直接等于 `Σweight`），
> 而 `estimated_change_pct` 才是真正的百分数。这是历史命名遗留，下游计算时务必区分：
> 判断阈值用 `coverage_pct >= 0.8`，而不是 `>= 80`。

### 2.2 时间字段是本地时间，无时区

所有时间由 `datetime.now().isoformat()` 生成，格式 `YYYY-MM-DDTHH:MM:SS`：

```
2026-10-28T09:36:24        ← 本地时间，无 +08:00 后缀，秒精度
2026-09-30                 ← report_date 只有日期
```

这意味着：

- 直接与 SQLite 的 `datetime('now')` 比较会差 8 小时（后者是 UTC），必须加 `'localtime'`：
  `WHERE ts >= datetime('now','localtime','-1 day')`
- 服务迁移到其他时区的机器后，历史数据与新数据的 `ts` 不可直接按"交易日"分组
- 字符串可直接做 `>=`、`<` 比较和 `ORDER BY`（ISO8601 字典序 = 时间序），无需转换

### 2.3 NULL 不代表 0

`estimated_nav` 为 NULL 表示"没有估值"，与"估值为 0"是两回事。同理
`coverage_pct` 为 NULL 表示该行不是合成路径产出（`api` / `fallback` 路径没有这个概念）。

聚合时要注意 SQL 的 NULL 语义：`AVG()` / `COUNT(col)` 会**跳过** NULL 行，
所以 `AVG(estimated_change_pct)` 的分母只是"有值的行数"，把不同 `source`、
不同 `status` 的行混在一起算平均会得到无意义的结果。做统计前先
`WHERE status = 'available'`，并按 `source` 分组。

---

## 3. `funds` — 基金基础信息（低频更新）

```sql
CREATE TABLE funds (
    fund_code             TEXT PRIMARY KEY,
    fund_name             TEXT,
    fund_type             TEXT,
    last_holdings_update  TEXT,
    UNIQUE(fund_code)
);
```

| 字段 | 类型 | 可空 | 含义 | 示例 |
|---|---|---|---|---|
| `fund_code` | TEXT | 否 | 6 位基金代码，主键，全局唯一 | `005827` |
| `fund_name` | TEXT | 是 | 基金全称，主要供日志与排查使用；来源为估值接口返回的 `SHORTNAME` 或 YAML 中的 `name` | `易方达蓝筹精选混合` |
| `fund_type` | TEXT | 是 | 粗分类，**由基金名称推测**（见 §7.4），仅供展示与统计，不参与估值决策 | `hybrid` |
| `last_holdings_update` | TEXT | 是 | 最近一次成功写入 `holdings_snapshot` 的时刻（ISO8601）。为 NULL 表示从未成功获取过持仓 | `2026-10-28T09:36:25` |

**写入时机**：

- `upsert_fund()`：每轮估值后按 `fund_code` 插入或更新，`fund_name` / `fund_type` 用
  `COALESCE(excluded.x, funds.x)` 合并，即**新值为空时保留旧值**，不会被 NULL 覆盖
- `save_holdings_snapshot()`：写入持仓成功后同步刷新 `last_holdings_update`

> `PRIMARY KEY` 与 `UNIQUE(fund_code)` 对同一列重复声明，是需求文档原样保留的写法，
> SQLite 会自动忽略冗余索引，无害。可通过该字段判断某基金是否"从未拿到过持仓"。

---

## 4. `holdings_snapshot` — 持仓快照（审计用）

每次成功拉取持仓后**按行保存**，一只基金 10 只重仓股 = 10 行。用于审计、覆盖率校准，
以及 24 小时内复用快照（避免每次轮询都抓页面）。

```sql
CREATE TABLE holdings_snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code      TEXT NOT NULL,
    snapshot_time  TEXT NOT NULL,
    asset_code     TEXT NOT NULL,
    asset_name     TEXT,
    weight         REAL,
    asset_type     TEXT DEFAULT 'stock',
    report_date    TEXT
);
```

| 字段 | 类型 | 可空 | 含义 | 示例 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | 自增主键。**同一快照重写时 id 会变化**（先删后插），不可作为稳定标识 | `1024` |
| `fund_code` | TEXT | 否 | 所属基金代码，与 `funds.fund_code` 对应（无外键约束） | `005827` |
| `snapshot_time` | TEXT | 否 | 本次抓取时刻（本地时间）。同一基金同一秒的整批记录共享同一个值，构成一次"快照" | `2026-10-28T09:36:25` |
| `asset_code` | TEXT | 否 | 持仓标的代码，**保留数据源原始形态**（6 位数字，港股为 5 位） | `600519`、`00700` |
| `asset_name` | TEXT | 是 | 标的名称，来自披露页面 | `贵州茅台` |
| `weight` | REAL | 是 | 占基金净值比例，**小数形式** | `0.0637`（= 6.37%） |
| `asset_type` | TEXT | 是 | 标的类型，默认 `stock`，取值见 §7.5 | `stock` |
| `report_date` | TEXT | 是 | **季报报告期**（如 `2026-09-30`），不是抓取时间。用于判断持仓是否过期。该列在需求基础上额外增加，旧库会自动 `ALTER TABLE` 补列 | `2026-09-30` |

**写入语义**：

1. 写入前先 `DELETE WHERE fund_code = ? AND snapshot_time = ?`，再批量 `INSERT`，保证幂等
2. 读取时 `latest_holdings()` 取 `snapshot_time >= now - 24h` 且**时间最新的一批**记录
3. `weight` 理论上可空，实际解析失败的行会被跳过，不会写入 NULL 权重

**用途**：`SELECT SUM(weight) FROM holdings_snapshot WHERE fund_code = ?` 即可得到该基金
前十大合计占净值比例——这是设置 `max_holdings_pct` 的**唯一可靠依据**（而非拍脑袋填 0.8）。

> ⚠️ 该表只增不删，每次抓取都追加。一只基金每天最多新增一次（24 小时快照复用），
> 长期运行一年约 365 × 10 = 3650 行/基金。见 §11 清理建议。

---

## 5. `valuation` — 估值结果（核心输出）

系统的唯一业务产出。每只基金每次轮询写一行，正常交易日每只基金约 240 行/天。

```sql
CREATE TABLE valuation (
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
```

| 字段 | 类型 | 可空 | 含义 | 示例 |
|---|---|---|---|---|
| `id` | INTEGER | 否 | 自增主键。upsert 命中唯一键时**只更新不换行，id 保持不变** | `3021` |
| `fund_code` | TEXT | 否 | 基金代码 | `005827` |
| `ts` | TEXT | 否 | **估值计算时刻**（本地时间，秒精度）。与 `fund_code` 组成唯一键 | `2026-10-28T09:36:24` |
| `estimated_nav` | REAL | 是 | 估算单位净值，统一保留 4 位小数。`failed` / `not_supported` 时为 NULL | `1.3809` |
| `estimated_change_pct` | REAL | 是 | **估算涨跌幅，百分数**（`-0.32` 表示跌 0.32%）。`GSZ` 为空时为 NULL；**例外**：`GZTIME` 判定为隔夜旧值时，该列会保留接口涨跌幅但 `status` 置为 `not_supported`，使用前必须看 `status` | `-0.32` |
| `base_nav` | REAL | 是 | 基准单位净值，即"前一交易日净值"，估算的起算点，保留 4 位小数 | `1.3853` |
| `coverage_pct` | REAL | 是 | **小数**。仅 `synthetic` 路径有值：实际参与计算的重仓股权重合计（已剔除无行情的个股）。`api` / `fallback` 为 NULL | `0.4262` |
| `confidence` | TEXT | 是 | 置信度分档，见 §7.3。`not_supported` / `failed` 时为 NULL | `low` |
| `source` | TEXT | 是 | **估值来源路径**，见 §7.2。这是判断数据可信度的首要字段 | `synthetic` |
| `status` | TEXT | 是 | 结果状态，见 §7.1。**区分"失败"与"本就无估值"的关键** | `available` |
| `data_source` | TEXT | 是 | 自由文本，记录具体用了哪些数据源，见 §8。**仅用于诊断，勿用于程序判断** | `eastmoney:mobile+tencent` |
| `error_msg` | TEXT | 是 | 失败或降级原因。`available` 时也可能有值（如走 fallback 时说明为何降级） | `使用代理ETF估算（002611 持仓获取失败: ...）` |

**写入语义**：

```sql
INSERT ... ON CONFLICT(fund_code, ts) DO UPDATE SET ...
```

- 同一基金同一秒重复计算 → **更新而非新增**，不会产生重复行
- `UNIQUE(fund_code, ts)` 是幂等的基础，也意味着**同秒两次不同结果只保留最后一次**
- 写入失败只记日志（`LOGGER.error`），不向上抛异常，不中断整批任务
- 净值类字段是 `REAL`（IEEE 754 双精度），代码按 4 位小数舍入，但**尾部零不会被保留**：
  `2.7300` 存进去读出来是 `2.73`。需要固定显示位数请在查询里格式化，
  不要期望数据库替你补齐。

**核心字段的关系**：

```
status='available'     → estimated_nav 非空，可用
status='not_supported' → estimated_nav 为 NULL，接口明确无估值（主动基金常态），不告警
status='failed'        → estimated_nav 为 NULL，所有数据源不可用，需要告警

source='api'       → 第三方直接给的权威估值，confidence 固定 high
source='synthetic' → 自己按持仓加权算的近似值，必须看 coverage_pct + confidence
source='fallback'  → 用代理 ETF 近似，confidence 固定 low，误差最大
```

净值与涨跌幅的换算关系：

- `synthetic` / `fallback`：一定是 `estimated_nav = base_nav × (1 + estimated_change_pct/100)`，
  因为净值是由涨跌幅**反推**出来的
- `api`：`estimated_nav` 直接取接口的 `GSZ`，是**官方值**；只有在接口缺 `GSZ` 时才用
  `base_nav × (1 + GSZZL/100)` 补齐。因此这两列在 `api` 路径下**不保证严格满足**上述等式
  （接口自身的舍入导致），不要用等式做数据校验

---

## 6. 索引

| 索引 | 列 | 支撑的查询 |
|---|---|---|
| `idx_valuation_code_ts` | `valuation(fund_code, ts)` | 单基金时间序列查询、`latest` 取数 |
| `idx_valuation_ts` | `valuation(ts)` | 全市场按时间扫描、健康度统计、按日清理 |
| `idx_holdings_code_time` | `holdings_snapshot(fund_code, snapshot_time)` | 24 小时快照复用查询 |

`funds.fund_code` 由主键自带索引。`holdings_snapshot` 未在 `id` 之外单独建索引，因为
查询几乎都带 `fund_code`。

---

## 7. 枚举字典

### 7.1 `valuation.status`

| 值 | 含义 | `estimated_nav` | 下游处理 |
|---|---|---|---|
| `available` | 成功算出估值 | 非空 | 正常使用，按 `source`/`confidence` 权衡 |
| `not_supported` | 数据源明确表示"无实时估值"（主动基金已被监管取消盘中估值），**合法业务状态** | NULL | 静默跳过，**不应告警** |
| `failed` | 所有数据源均不可用 | NULL | 需要告警，看 `error_msg` |

### 7.2 `valuation.source`

| 值 | 含义 | 典型基金 | 可信度 |
|---|---|---|---|
| `api` | 直接消费天天基金/新浪返回的 `GSZ`+`GSZZL` | 场内 ETF、LOF、部分指数基金 | 最高，权威 |
| `synthetic` | 前十大权重 × 个股实时涨跌，归一化合成 | 主动权益、场外指数基金 | 方向性参考，取决于 `coverage_pct` |
| `fallback` | 持仓不可得，用代理 ETF 涨跌 × 0.95 近似 | ETF 联接基金 | 最低，`confidence` 固定 `low` |

### 7.3 `valuation.confidence`

`confidence` 由路径决定，只有 `synthetic` 需要计算：

| 路径 | `confidence` |
|---|---|
| `api` | **固定 `high`**（官方盘中估值，无持仓穿透概念，`coverage_pct` 为 NULL） |
| `fallback` | **固定 `low`**（代理 ETF + 95% 仓位假设） |
| `synthetic` | 按 `coverage_pct` 与配置中 `max_holdings_pct` 的比值判定 |

`synthetic` 的判定条件：

| 值 | 判定条件 |
|---|---|
| `high` | `coverage_pct >= max_holdings_pct` |
| `medium` | `coverage_pct >= 0.75 × max_holdings_pct` |
| `low` | 低于上述任一条件，或有效持仓数为 0 |

注意：`confidence` 是**相对于你配置的基线**的标签，不是绝对精度指标。同一只基金
把 `max_holdings_pct` 从 0.8 改成 0.5，`estimated_nav` 一分不变，只有该标签会变。

### 7.4 `funds.fund_type`

由 `guess_fund_type()` 按**基金名称**推断（名称是唯一稳定的线索，持仓类型要到首次抓取后才知道）。
判定顺序：`联接`→`ETF`→`LOF`→`指数`→`债`/`货币`→`股票`→`混合`→持仓含股票→兜底。

| 值 | 含义 |
|---|---|
| `etf` | ETF 及其联接基金（名称含"ETF"或"联接"） |
| `lof` | 上市开放式基金（名称含"LOF"） |
| `index` | 指数基金（名称含"指数"但非 ETF/LOF） |
| `bond` | 债券型 / 货币型 |
| `equity` | 股票型，或持仓中确认含股票 |
| `hybrid` | 混合型 |
| `unknown` | 无法推断 |

### 7.5 `holdings_snapshot.asset_type`

| 值 | 含义 |
|---|---|
| `stock` | 个股（默认值） |
| `index` | 指数 |
| `etf` | ETF 份额 |

---

## 8. `data_source` 命名规则（自由文本）

格式为 `{主源}` 或 `{主源}:{子源}+{行情源1}+{备用行情源}`，由各数据源实现自行拼接：

| 实际值 | 拆解 |
|---|---|
| `tiantian` | direct 路径，天天基金估值接口 |
| `eastmoney:mobile+tencent` | 合成路径，东财移动端持仓 + 腾讯行情 |
| `tiantian:f10+tencent` | 合成路径，天天基金档案接口持仓 + 腾讯行情 |
| `fallback:sh518880+tencent` | fallback 路径，代理 ETF `sh518880` + 腾讯行情 |
| `tiantian,sina` | direct 路径**全部估值源都未返回该基金**（逗号分隔的源列表），此时 `status='failed'` |

> 这是**诊断信息，不是枚举**。格式会随数据源重构变化，程序判断请用 `source` 字段，
> 不要 `LIKE '%tencent%'` 做业务逻辑。

---

## 9. 常用查询

```sql
-- 各基金当日最新一条（下游最主要的使用方式）
SELECT v.* FROM valuation v
JOIN (SELECT fund_code, MAX(ts) AS mx FROM valuation
      WHERE ts LIKE '2026-10-28%' GROUP BY fund_code) t
  ON v.fund_code = t.fund_code AND v.ts = t.mx;

-- 当日涨跌走势（单基金时序）
SELECT ts, estimated_nav, estimated_change_pct, confidence
FROM valuation WHERE fund_code = '005827' AND ts LIKE '2026-10-28%'
ORDER BY ts;

-- 校准 max_holdings_pct：该基金前十大合计占净值比例
SELECT fund_code, report_date, COUNT(*) AS n, ROUND(SUM(weight), 4) AS top10_total
FROM holdings_snapshot WHERE fund_code = '005827'
GROUP BY fund_code, report_date;

-- 健康度：近 24h status / source / confidence 分布
SELECT status, source, confidence, COUNT(*) AS cnt FROM valuation
WHERE ts >= datetime('now','localtime','-1 day')
GROUP BY status, source, confidence ORDER BY cnt DESC;

-- 失败排查
SELECT ts, fund_code, source, error_msg FROM valuation
WHERE status = 'failed' ORDER BY ts DESC LIMIT 50;
```

---

## 10. 数据量与清理

单只基金正常交易日写入量：`4 小时 × 60 分钟 = 240 行/天`（默认 60 秒间隔）。
10 只基金一年约 60 万行，SQLite 完全可承受，但建议按需归档：

```sql
-- 删除 90 天前的估值（先备份文件再执行）
DELETE FROM valuation WHERE ts < datetime('now','localtime','-90 day');

-- 持仓快照只保留最近 30 天抓取的记录（24h 复用机制只用到 1 天内，其余仅供审计）
DELETE FROM holdings_snapshot WHERE snapshot_time < datetime('now','localtime','-30 day');

VACUUM;   -- 回收空间，WAL 模式下需在无写入时进行
```

> `VACUUM` 执行期间需要独占，建议在非交易时段做。WAL 模式下还会产生
> `-wal` / `-shm` 附属文件，属正常现象，不要单独删除（正常关闭连接时会自动合并）。

---

## 11. 易错点清单

| 现象 | 原因 | 正确做法 |
|---|---|---|
| 判断覆盖率阈值全错 | `coverage_pct` 是小数不是百分数 | 用 `>= 0.8`，不要 `>= 80` |
| 时间过滤少 8 小时数据 | `ts` 是本地时间，`datetime('now')` 是 UTC | 加 `'localtime'` |
| `AVG(estimated_change_pct)` 失真 | `failed` / `not_supported` 行为 NULL，且不同 source 混在一起 | 加 `WHERE status='available'` 并按 `source` 分组 |
| 以为 `not_supported` 是故障 | 主动基金本就没有盘中估值 | 只对 `status='failed'` 告警 |
| 以为 `confidence=low` 是数据错误 | 由 `max_holdings_pct` 基线决定 | 用 §9 的 SQL 校准阈值 |
| `holdings_snapshot.id` 作为稳定 ID | 同快照重写时先删后插，id 会变 | 用 `(fund_code, snapshot_time, asset_code)` 定位 |
| 直接删 `-wal` 文件 | 误以为它是垃圾文件 | 先 `PRAGMA wal_checkpoint(TRUNCATE)` 或正常关闭连接 |
| 用 `data_source` 做业务判断 | 它是自由文本，格式可能变 | 用 `source` 字段 |

---

## 12. 与代码的对应关系

| 表 | 写入方法 | 读取方法 |
|---|---|---|
| `funds` | `upsert_fund()`、`save_holdings_snapshot()`（刷新时间戳） | — |
| `holdings_snapshot` | `save_holdings_snapshot()` | `latest_holdings()` |
| `valuation` | `save_valuation()`、`save_valuations()` | `recent_valuations()`、`distribution()` |

所有 SQL 字符串集中在 `fund_valuation/db.py`，上层模块不出现 SQL。若需修改表结构，
只改该文件的 `SCHEMA` 并在 `_migrate()` 中补充旧库兼容逻辑。
