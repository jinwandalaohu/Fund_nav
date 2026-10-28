# 数据获取与计算逻辑详解

本文按**一次轮询的执行顺序**拆解数据获取与计算过程，每一步都标注源码位置，便于对照阅读。

> 与其它文档的分工：
> - `README.md` 讲**是什么**（策略、配置项、算法概念）
> - `DATABASE.md` 讲**存什么**（字段含义、枚举、SQL）
> - 本文讲**怎么跑**（调用链、请求参数、解析规则、公式、失败降级）

---

## 1. 全景：一次轮询的执行时序

```
main.main()                                      main.py:154
 ├─ load_config(yaml)                             config.py:86     ← 校验失败直接退出(return 2)
 ├─ Database(db_path)                             db.py:92         ← 建表 + 迁移 + WAL
 ├─ build_engine(config, db)                      main.py:43
 │   ├─ HttpClient(timeout, retries, ua)                           ← 全局唯一，线程隔离 Session
 │   ├─ DirectValuator([tiantian, sina])                           ← 估值源链
 │   └─ SyntheticValuator([持仓主, 备], [tencent, sina], nav)       ← 持仓/行情/净值源链
 │
 └─ engine.run_once()                             engine.py:72
     ├─ ① _ensure_funds()                         engine.py:206    ← 配置里的基金写入 funds
     ├─ ② _run_direct(now)                       engine.py:90     ← 批量取 GSZ/GSZZL（一次 HTTP）
     ├─ ③ _evaluate_all(now, direct_results)     engine.py:108     ← 按 mode 分派，线程池并发
     │     ├─ _evaluate_one()                    engine.py:140
     │     │    ├─ DIRECT    → 用 ② 的结果，或 failed
     │     │    ├─ SYNTHETIC → 跳过 ②，直接合成
     │     │    └─ AUTO      → ② 可用则直接用；否则降级合成
     │     └─ _try_synthetic()                   engine.py:171
     │          └─ SyntheticValuator.evaluate()  synthetic.py:188
     │               ├─ 持仓：DB 快照 → 东财移动端 → 天天档案
     │               ├─ 净值：pingzhongdata → lsjz
     │               ├─ 行情：腾讯 → 新浪
     │               └─ 加权：aggregate_holdings()  synthetic.py:88
     └─ ④ _persist(outcomes)                     engine.py:215    ← 快照 + 估值批量落库
```

**关键设计**：② 是**一次批量请求搞定所有基金**的估值（`FCODES` 逗号拼接），
③ 才是逐只基金的重活（抓持仓、抓行情）。因此 direct 路径的耗时与基金数几乎无关，
而 synthetic 路径是 `O(基金数 × 网络往返)`，这也是引入线程池（`max_workers`）的原因。

---

## 2. 阶段 0：依赖装配与源链构造

`build_engine()` 把三样东西组装起来：

```python
http = HttpClient(timeout=runtime.request_timeout, retries=runtime.retry_times,
                  user_agent=runtime.user_agent)          # main.py:50
```

**全局只有一个 `HttpClient`**，所有数据源共用。它内部按线程持有独立 `requests.Session`
（`http.py:82`，Session 非线程安全，合成阶段会并发抓行情）。

源链是硬编码优先级，配置里不能调顺序（`registry.py`）：

| 环节 | 优先级链 | 构造位置 |
|---|---|---|
| 直接估值 | `tiantian` → `sina` | `registry.build_valuation_sources` |
| 持仓 | 主源 → 另一个源（自动互为备份） | `registry.build_holdings_source` |
| 个股行情 | `tencent` → `sina` | `registry.build_quote_sources` |
| 基准净值 | `tiantian`（内含 pingzhongdata / lsjz 双策略） | `registry.build_nav_source` |

### ⚠️ `holdings_source` 的实际行为（配置语义与实现有偏差）

`main._primary_holdings_source()`（`main.py:70`）统计**所有基金**的 `holdings_source`，
取出现次数最多的那个，构造出**一条全局源链**：

```python
counts = {}
for fund in config.funds:
    counts[fund.holdings_source] = counts.get(fund.holdings_source, 0) + 1
return max(counts, key=counts.get)          # 少数服从多数
```

而 `SyntheticValuator` 收到的就是这个固定链，**`fund.holdings_source` 从未被使用**
（`synthetic.py` 全文没有该字段的引用）。

后果：5 只基金中 4 只写 `eastmoney`、1 只写 `tiantian`，那"1 只"实际仍会先走东财。
配置项看起来是"每只基金单独指定"，实际是"全局投票"。这是当前实现的已知偏差，
修法见 §13。

---

## 3. 阶段 1：调度——什么时候才采数据

`Scheduler.run_forever()`（`scheduler.py:48`）：

```python
while self._running:
    if trade_only and not calendar.is_trade_time(now):
        wait = min(calendar.seconds_until_next_window(now), 3600.0)
        self._sleep(wait)          # 非交易时段：长睡，不采集
        continue
    engine.run_once()              # 采集 + 计算 + 落库
    self._sleep(max(0.0, interval - elapsed))   # 扣掉本轮耗时，保持固定节拍
```

- 交易时段：`09:30–11:30`、`13:00–15:00`，且当日非周末、非休市日（`calendar.py:94`）
- 非交易时段：睡到下一个窗口（上限 1 小时，便于响应配置变更）
- 长睡眠切成 300 秒片段（`scheduler.py:20`），保证 Ctrl+C 及时响应
- `--once` 与 `--force` 绕过时段判断（`main.py:166` 用 `dataclasses.replace` 改运行时配置）

**为什么要在意这个**：非交易时段行情是静止的，继续采集只会把同一组价格写成一堆重复行，
既浪费请求也污染时序数据。

---

## 4. 阶段 2：直接取值（DirectValuator）

### 4.1 请求

```python
BASE_URL = "https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast"
params = {"FCODES": ",".join(codes), "FIELDS": "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE"}
headers = {"Referer": "https://fund.eastmoney.com/"}     # 缺 Referer 会被拒
```

`FIELDS` 显式裁剪只取 7 个字段——接口全字段返回体积是裁剪后的数倍，而这里只需要这些。

### 4.2 分批与逐级补取

```python
for source in self._sources:                      # direct.py:75
    if not pending: break
    fetched = self._fetch(source, pending)        # 内部按 batch_size=50 切批
    quotes.update(fetched)
    pending = [c for c in pending if c not in quotes]   # 只把"没拿到"的交给下一个源
```

**不是整批重试**：主源返回 48/50 只，就只拿剩下 2 只去问新浪。这能显著减少无效请求。

单批失败只 `LOGGER.warning` 不抛出（`direct.py:102`），一只基金的问题不影响同批其它基金。

### 4.3 响应解析

`_extract_rows()`（`tiantian.py:229`）对包裹层做兼容：`Datas` / `datas` / `data` / `Data`，
列表或嵌套字典都试一遍。**实测该接口返回小写 `data`**，写死 `Datas` 会全军覆没。

字段映射（`tiantian.py:72`）：

| 接口字段 | 模型字段 | 处理 |
|---|---|---|
| `FCODE` | `fund_code` | 字符串去空格，空则丢弃该行 |
| `SHORTNAME` | `fund_name` | 用于回填 `funds.fund_name` |
| `GSZ` | `gsz` | `safe_float`，`""`/`"-"`/`"null"` 一律转 `None` |
| `GSZZL` | `gszzl` | 同上，单位是**百分数** |
| `GZTIME` | `gz_time` | 正则去掉尾部的 `:ss`（只在确实带秒时裁，避免 `15:00`→`15`） |
| `NAV` | `nav` | 前一交易日单位净值 |
| `PDATE` | `nav_date` | 净值日期 |

### 4.4 结果三分支（`direct.py:108`）

```
quote is None            → status=failed        "所有直接估值源均未返回该基金数据"
quote.is_empty           → status=not_supported  (GSZ 与 GSZZL 同时为 None)
GZTIME 不是当日           → status=not_supported  并保留 gszzl，error_msg 说明是隔夜旧值
否则                      → status=available，confidence=high，source=api
```

`is_empty` 判定是**合法业务状态**，不是错误：主动权益基金的盘中估值已被监管下线，
接口返回空字段是正常现象。这里绝不抛异常、绝不重试，交给引擎决定是否降级。

`GZTIME` 校验（`runtime.validate_gztime`，默认开）解决的问题：收盘后接口仍会返回**上一交易日**
的估值，若直接入库，下游会把隔夜旧值当成今日实时行情。

### 4.5 净值与涨跌幅互相补齐（`direct.py:159`）

```python
if estimated_nav is None and change_pct is not None and nav:
    estimated_nav = nav * (1 + change_pct / 100.0)     # 只有涨跌幅 → 反推净值
if change_pct is None and estimated_nav is not None and nav:
    change_pct = (estimated_nav / nav - 1) * 100.0     # 只有净值 → 反推涨跌幅
```

保证 `estimated_nav` 与 `estimated_change_pct` 两列**口径一致**，不会一列有值一列 NULL。

---

## 5. 阶段 3：持仓获取（synthetic 路径第一步）

### 5.1 先查 24 小时快照（`synthetic.py:259`）

```python
cached = self._snapshot_loader(fund_code, self._snapshot_max_age_hours)   # 24h
if cached and cached.holdings:
    return cached                      # 命中则完全跳过网络
```

`snapshot_loader` 注入的是 `db.latest_holdings`（`db.py:243`），查 `holdings_snapshot` 表
中 `snapshot_time >= now - 24h` 的**最新一批**记录。

**为什么是 24 小时**：季报每季度才更新一次，持仓在一天内几乎不变。每分钟抓一次页面既浪费
又容易被限流。实际运行中可以看到 `data_source` 从 `eastmoney:mobile+tencent`
变为 `db:snapshot+tencent`，就是命中了快照。

### 5.2 主源：东财移动端 JSON（`eastmoney.py:43`）

```
GET https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition
    ?FCODE=005827&deviceid=Wap&plat=WAP&product=EFund&version=2.0.0
Headers: 移动端 iPhone UA + Referer: https://h5.1234567.com.cn/
```

三个必须点，缺一不可：

1. **移动端 UA**：用桌面 UA 会返回 `ErrCode=61136403 网络繁忙`
2. **H5 Referer**：同上
3. `plat=WAP` / `deviceid=Wap`：标识来自 H5 端

错误判定（`eastmoney.py:82`）：

```python
if payload.get("Success") is False or payload.get("ErrCode") not in (0, None):
    raise DataSourceError(...)          # 带上 ErrCode 与 ErrMsg，便于排查
```

解析（`eastmoney.py:89`）：取 `Datas.fundStocks[]`，逐项映射

| 接口字段 | 含义 | 处理 |
|---|---|---|
| `GPDM` | 股票代码 | 保留原始形态（`600519` / `00700`），**不加前缀** |
| `GPJC` | 股票简称 | 名称 |
| `JZBL` | 占净值比例 | `_weight_to_decimal()` 按声明的 `weight_unit` 换算 |

权重单位由**数据源显式声明**，解析器不做任何推断：

```python
# models.py — 单位是接口字段的固有属性，不是数值的性质
class WeightUnit(StrEnum):
    PERCENT = "percent"    # "9.53" / "9.53%"  → 9.53%
    DECIMAL = "decimal"    # 0.0953            → 9.53%

# datasource/base.py — 每个持仓源声明一次，解析处统一调 _weight_to_decimal()
class EastmoneyHoldingsSource(HoldingsSource):
    weight_unit = WeightUnit.PERCENT

# utils/html.py — 换算只认声明
return number / 100.0 if unit is WeightUnit.PERCENT else number
```

**为什么不能按数值大小推断**：联接基金 95% 资产在目标 ETF 里，股票明细只剩残余仓位，
`JZBL` 会给出 `0.52`（0.52%）。旧实现用「>1 才除以 100」的判据，会把它当成 52%，
前十大合计变成 253%，`coverage_pct > 1` 这种不可能值反而被标成 `high` 置信度。
单位猜错是 100 倍偏差，且结果"有数"，属于最难发现的静默错误，
因此把口径提升为数据源的一等声明，新增源必须核对真实返回值后再声明。

报告期取 `payload["Expansion"]`（如 `2026-09-30`）。最后按权重降序、取前 10 只。

### 5.3 备源：天天基金档案接口（`tiantian.py:100`）

```
GET https://fundf10.eastmoney.com/FundArchivesDatas.aspx
    ?type=jjcc&code=005827&topline=10&year=&month=&rt=<时间戳>
```

响应是 JS 赋值语句，不是纯 HTML：

```javascript
var apidata={ content:"<table>...<tr><td>600519</td>...</tr></table>",arryear:[...] };
```

处理链（三步）：

1. `_API_DATA_PATTERN` 正则抠出 `content:"..."`（`tiantian.py:29`）
2. 反转义：`\"` → `"`、`\/` → `/`，去掉 `\r\n\t`
3. `parse_holdings_table()` 交给 BeautifulSoup 解析

**为什么不用东财 PC 的 F10 页面**（实测结论，`eastmoney.py:3` 有记录）：
`ccmx_{code}.html` 已改为 JS 渲染，静态 HTML 里没有表格；
`api.fund.eastmoney.com/f10/ccmx` 已 404。所以静态解析落在档案接口上，与移动端 JSON 互为备份。

### 5.4 表格解析：按表头定位列，不写死下标（`html.py:83`）

```python
_CODE_HEADERS   = ("股票代码", "债券代码", "证券代码", "代码")
_NAME_HEADERS   = ("股票名称", "债券名称", "证券名称", "名称")
_WEIGHT_HEADERS = ("占净值比例", "占净值比率", "市值占净值比")
```

遍历页面所有 `<table>`，找到同时含"代码"类表头与"占比"类表头的那个，再逐行解析：

- 代码列必须匹配 `[0-9A-Za-z.]{2,12}`，过滤掉"合计"这类汇总行
- 权重为 0 或无法解析的行**直接丢弃**（没有权重就无法参与加权，计入分母只会引入噪声）
- 多个表格命中时，取**行数最多的**那个（页面顶部常有迷你持仓卡片）
- 最后降序取前 10 只

报告期解析支持三种格式（`html.py:138`）：

| 页面写法 | 输出 |
|---|---|
| `报告期：2026-09-30` | `2026-09-30` |
| 任意 `2026-09-30` 形态 | 原样 |
| `2026年3季度` | `2026-09-30`（按季度末折算） |

### 5.5 两个源都失败

```python
raise DataSourceError(self.name, f"{fund_code} 持仓获取失败: " + " | ".join(errors))
```

随后由 `_etf_fallback` 或引擎接管（见 §9）。**单个源失败不会中断整只基金**，
两个都失败才向上抛。

---

## 6. 阶段 4：基准净值（`tiantian.py:145`）

两次尝试，都失败则本次估值放弃（没有基准净值就无法把涨跌幅换算成净值）。

**策略 1：`pingzhongdata`（首选）**

```
GET https://fund.eastmoney.com/pingzhongdata/{code}.js?v=<毫秒时间戳>
```

从 `var Data_netWorthTrend = [{x:毫秒时间戳,y:净值},...];` 中**倒序遍历**，
取第一个 `y > 0` 的点。倒序是因为序列按时间正序返回，最后一个才是最新净值；
`y > 0` 过滤掉数据缺口。

日期换算要注意：`x` 是毫秒时间戳，**需加 8 小时**再取日期（`tiantian.py:290`），
否则 UTC 换算会把 `2026-10-27` 的净值标成 `2026-10-26`。

**策略 2：`lsjz`（兜底）**

```
GET https://api.fund.eastmoney.com/f10/lsjz?fundCode=005827&pageIndex=1&pageSize=2
```

取 `Data.LSJZList[].DWJZ` 中第一个有效值。`pageSize=2` 是因为长假后最新一条可能为空，
多取一条有冗余。

---

## 7. 阶段 5：个股实时行情

### 7.1 代码归一化（`http.py:203`）——最易踩的坑

持仓表里存的是**原始代码**（`600519`、`00700`），行情接口要求**带市场前缀**（`sh600519`、`hk00700`）。
`normalize_code()` 的规则：

| 输入 | 规则 | 输出 |
|---|---|---|
| `600519` / `510300` / `518880` | 6 位、首位 `5`/`6`/`9` → 沪市 | `sh600519` |
| `000001` / `300502` / `159937` | 6 位、首位 `0`/`1`/`2`/`3` → 深市 | `sz000001` |
| `430047` / `830799` | 6 位、首位 `4`/`8` → 北交所 | `bj430047` |
| `00700` | 5 位数字 → 港股 | `hk00700` |
| `sh600519` / `SH600519` | 已带前缀 → 原样（转小写） | `sh600519` |
| `600519.SH` / `700.HK` | 后缀式 → 剥离后按上述规则 | `sh600519` / `hk00700` |
| `AAPL` | 纯字母 → 美股 | `usAAPL` |

### 7.2 别名映射：为什么必须有（`synthetic.py:286`）

行情源返回的字典**以归一化代码为键**，而持仓对象里存的是原始代码。直接查表会全部落空，
表现为"所有重仓股都没有行情"这种误导性错误。

```python
alias = {normalize_code(raw): raw for raw in asset_codes}     # hk00700 -> 00700
quotes = source.fetch_quotes(list(alias))                     # 用归一化键请求
by_original = {alias[k]: v for k, v in quotes.items() if k in alias}   # 换回原始键
```

实测 005827 的前十大里有 4 只港股（`00700`、`00883`、`09988`、`09987`，合计占净值 21.11%）。
若完全没有这层映射，10 只票会**全部查不到**（`coverage` 归零，直接触发 fallback 或 failed）；
若行情源不支持港股，`coverage` 则从 42.62% 降到 21.51%。

### 7.3 腾讯行情（`tencent.py:38`）主源

```
GET https://qt.gtimg.cn/q=sh600519,hk00700,sz000858     （GBK 编码）
```

返回 `v_sh600519="1~贵州茅台~600519~1338.00~1355.90~...";`，用正则逐行提取
（`_QUOTE_LINE`），**绝不用 `eval`** —— 响应来自外网，`eval` 等于把执行权交给对方。

字段位置（按 `~` 分割）：

| 下标 | 内容 | 用途 |
|---|---|---|
| `[1]` | 名称 | 仅展示 |
| `[3]` | 现价 | 参与计算 |
| `[4]` | 昨收 | 参与计算 |
| `[30]` | 时间戳 | 仅记录 |
| `[32]` | 涨跌幅（接口自带） | 存入 `change_pct_hint`，**不采用** |

涨跌幅一律**自己现算**：`(price - prev_close) / prev_close × 100`（`models.py:Quote.change_pct`）。
不信任接口字段是因为口径不统一（有的给百分数、有的给小数、有的四舍五入到 2 位），
自己算保证与 `prev_close` 严格自洽。

`price <= 0` 或 `prev_close <= 0` 的行会被丢弃（`tencent.py:84`）——停牌或未开盘时常返回 0，
这类数据参与加权会把"没交易"算成"跌 100%"。

### 7.4 新浪行情（`sina.py:40`）备源

```
GET https://hq.sinajs.cn/list=sh600519,sz000858    （GBK）
Headers: Referer: https://finance.sina.com.cn/     ← 2021 年起校验，缺失返回 403
```

返回 `var hq_str_sh600519="贵州茅台,1339.00,1355.90,1338.00,...";`（逗号分隔）：

| 下标 | 内容 |
|---|---|
| `[0]` | 名称 |
| `[1]` | 今开 |
| `[2]` | **昨收** |
| `[3]` | **现价** |

新浪的字段顺序与腾讯不同（腾讯 `[3]`=现价、`[4]`=昨收，新浪 `[2]`=昨收、`[3]`=现价），
必须分别处理，混用会算出方向相反的涨跌幅。

新浪源只处理 `sh`/`sz`/`bj`（`sina.py:52`）：港股/美股的新浪接口字段口径不同，
强行解析会算错，因此静默跳过交给腾讯源。

### 7.5 逐源补取

与估值源同样策略：腾讯返回 10 只就够，缺的才问新浪；两源都没有才抛
`DataSourceError("所有行情源均未返回有效报价")`。

注意：**单只股票缺行情不会抛异常**，它只是从 `aggregate_holdings` 的分母里剔除。

---

## 8. 阶段 6：加权计算（核心公式）

### 8.1 纯函数实现（`synthetic.py:88`）

```python
for holding in holdings:
    if holding.weight <= 0:            # 无权重，跳过
        continue
    quote = quotes.get(holding.asset_code)
    if quote is None or not quote.is_valid:    # 无行情或价格非法
        missing.append(holding)                # 记入缺失，不进分母
        continue
    total_weight   += holding.weight
    weighted_change += holding.weight * quote.change_pct
    used.append((holding, quote))

change_pct = weighted_change / total_weight    # ← 归一化
coverage   = total_weight
```

### 8.2 为什么必须除以 `total_weight`

前十大只占净值的一部分（本例 42.62%）。若直接取 `Σ(w×r)`，等于**假设剩余 57.38% 的仓位不涨不跌**，
结果被系统性压缩。数学上：

```
Σ(w × r) = 归一化结果 × coverage
```

以 005827 的真实盘口为例（2026-10-28 10:05 实测）：

| | 数值 |
|---|---|
| `Σ(w × r)` | **-0.4240%** |
| `coverage` | 0.4262 |
| 归一化 `Σ(w×r) / coverage` | **-0.9947%** |
| 低估幅度 | 57.4% ≈ `1 - coverage` |

低估幅度**恰好等于未覆盖比例**。这就是必须归一化的原因，也是 `coverage_pct` 必须落库的原因
——它是误差的量化指标，没有它就无法判断这个估值有多可信。

### 8.3 单只股票缺行情的处理

**从分母剔除，而不是按 0% 涨跌计入**：

- 剔除：`coverage` 变小，`change_pct` 仍是"已覆盖部分"的平均涨跌，语义正确
- 按 0% 计入：把"没有数据"错误地当成"没有波动"，会把结果往 0 拉

剔除的副作用是 `coverage` 下降 → 置信度可能降级，这是**正确的信号传递**：
能算的股票越少，结果越不可信。

### 8.4 置信度（`synthetic.py:127`）

```python
if usable_count <= 0 or coverage <= 0:  return LOW
if max_holdings_pct <= 0:               return LOW
ratio = coverage / max_holdings_pct
if ratio >= 1.0:                        return HIGH
if ratio >= 0.75 - 1e-9:                return MEDIUM   # 1e-9 容差防浮点误差
return LOW
```

`1e-9` 容差的原因：`0.60 / 0.80` 在浮点下是 `0.7499999999`，不加容差会把本该 `medium`
判成 `low`。

`max_holdings_pct` 是**基线**不是上限，只影响这张标签，不影响任何数值（详见 `DATABASE.md` §7.3）。

### 8.5 净值换算

```python
estimated_nav = nav.nav * (1 + aggregation.change_pct / 100.0)    # synthetic.py:226
```

最后统一 `round(..., 4)`。注意 `estimated_change_pct` 是**百分数**（`-0.9947`），
`coverage` 是**小数**（`0.4262`），两者单位不同。

---

## 9. 阶段 7：降级矩阵

`engine._evaluate_one()`（`engine.py:140`）用 `match` 语句分派：

| 配置 `mode` | direct 结果 | 最终行为 | 落库 `status` / `source` |
|---|---|---|---|
| `direct` | `available` | 直接用 | `available` / `api` |
| `direct` | `not_supported` | **不降级**，原样返回 | `not_supported` / `api` |
| `direct` | `failed` 或 None | 返回 failed，不降级 | `failed` / NULL |
| `synthetic` | 不请求 direct | 直接合成 | `available` / `synthetic` 或 `fallback` |
| `auto` | `available` | 直接用，省掉全部重活 | `available` / `api` |
| `auto` | `not_supported` | **降级合成** | `available` / `synthetic` 或 `fallback` |
| `auto` | `failed` | 降级合成 | 同上 |
| `auto` | 合成也失败 | 汇总两侧原因 | `failed`，`error_msg` 含 `direct: ...; synthetic: ...` |

`_try_synthetic` 的失败信息会**合并两段原因**（`engine.py:190`）：

```python
reason = f"direct: {direct_result.error_msg}; synthetic: {reason}"
```

排查时能一眼看出是"接口没估值"还是"持仓抓不到"。

### synthetic 内部的兜底（`synthetic.py:332`）

```
持仓拉不到（DataSourceError）        ┐
或所有行情源都挂（DataSourceError）  ├─→ 有 fallback_etf → 代理 ETF 涨跌 × 0.95，source=fallback
或 coverage <= 0（一只票都没行情）   ┘   └→ 无 fallback_etf → 抛 DataSourceError → failed
```

代理路径的计算：

```python
change_pct = quote.change_pct * self._etf_factor      # 0.95
estimated_nav = nav.nav * (1 + change_pct / 100.0)
confidence = Confidence.LOW                            # 固定 low
```

`0.95` 的由来：ETF 联接基金须 ≥90% 资产投目标 ETF，实际约 95%，剩余为现金与应收款，
不跟随涨跌。

**注意**：`fallback_etf` 只在"持仓彻底拿不到"时生效。持仓正常、只是覆盖率低，
仍然走正常合成路径，不会退化成代理。

---

## 10. 阶段 8：落库（`engine.py:215`）

```python
for outcome in outcomes:
    if outcome.snapshot is not None and outcome.snapshot.holdings:
        db.save_holdings_snapshot(outcome.snapshot)      # 先删后插，幂等
    db.upsert_fund(code, name, guess_fund_type(name))    # COALESCE 保留旧值
db.save_valuations(results)                              # executemany
```

三张表的写入顺序有依赖：先写持仓快照（顺带刷新 `funds.last_holdings_update`），
再写估值。估值用 `ON CONFLICT(fund_code, ts) DO UPDATE`，同秒重算不产生重复行。

`guess_fund_type()` 依据**名称**推断类型（`html.py:177`），判定顺序：
`联接` → `ETF` → `LOF` → `指数` → `债`/`货币` → `股票` → `混合`。

---

## 11. 常量与阈值总表

| 常量 | 值 | 位置 | 作用 |
|---|---|---|---|
| `batch_size` | 50 | 配置 | 批量估值接口单批上限 |
| `_MAX_CODES_PER_REQUEST` | 60 / 50 | `tencent.py:29` / `sina.py:21` | 行情单次请求代码数上限 |
| `ETF_POSITION_FACTOR` | 0.95 | `synthetic.py:51` | 代理 ETF 仓位折算 |
| `MEDIUM_RATIO` | 0.75 | `synthetic.py:54` | medium 档相对基线的比例 |
| `snapshot_max_age_hours` | 24 | `synthetic.py:173` | 持仓快照复用窗口 |
| `_MAX_SLEEP_SLICE` | 300s | `scheduler.py:20` | 长睡眠切片 |
| `request_timeout` | 8s | 配置 | 单次 HTTP 超时 |
| `retry_times` | 2 | 配置 | 重试次数（仅连接错误/超时/5xx） |
| `backoff` | 0.4 × 2^n | `http.py:186` | 指数退避：0.4s、0.8s |
| `max_workers` | 4 | 配置 | 合成路径并发线程数（1..32） |

**重试策略**（`http.py:160`）：只对 `Timeout` / `ConnectionError` / `5xx` 重试。
`4xx` 属于请求本身有问题（Referer 错、代码不存在），重试只会放大故障。

---

## 12. 一次真实轮询的数字复盘

005827 易方达蓝筹精选，2026-10-28 10:05 实测（持仓为 2026-09-30 季报）：

| 代码 | 名称 | 权重 | 现价 | 昨收 | 涨跌% | w×r |
|---|---|---|---|---|---|---|
| 600519 | 贵州茅台 | 6.37% | 1338.00 | 1355.90 | -1.32 | -0.08409 |
| 00883 | 中国海洋石油 | 6.22% | 23.88 | 24.20 | -1.32 | -0.08225 |
| 00700 | 腾讯控股 | 6.06% | 387.40 | 385.20 | +0.57 | +0.03461 |
| 09987 | 百胜中国 | 5.71% | 350.80 | 355.40 | -1.29 | -0.07391 |
| 000568 | 泸州老窖 | 4.58% | 69.35 | 70.85 | -2.12 | -0.09697 |
| 09988 | 阿里巴巴-W | 3.12% | 98.15 | 97.00 | +1.19 | +0.03699 |
| 600809 | 山西汾酒 | 2.89% | 109.09 | 113.32 | -3.73 | -0.10788 |
| 000858 | 五粮液 | 2.79% | 71.21 | 75.38 | -5.53 | -0.15434 |
| 600674 | 川投能源 | 2.55% | 17.67 | 17.71 | -0.23 | -0.00576 |
| 300502 | 新易盛 | 2.33% | 248.77 | 237.59 | +4.71 | +0.10964 |

```
total_weight   = 0.4262
weighted_change = -0.42395
change_pct     = -0.42395 / 0.4262 = -0.9947 %
base_nav       = 1.3734（2026-10-27 净值）
estimated_nav  = 1.3734 × (1 - 0.9947/100) = 1.3597
coverage       = 0.4262 → 0.4262 / 0.80 = 0.53 < 0.75 → confidence = low
```

值得注意的细节：

- 10 只票行情全部取到，`coverage` 等于持仓合计 42.62%，没有剔除任何一只
- 其中 4 只是港股，靠 `hk` 前缀 + 别名映射才拿到（少了这层，覆盖率会掉到 2%）
- 沪市、深市、港股三种代码形态混在一张表里，`normalize_code` 全部正确识别
- 加权利空主要来自白酒（五粮液 -5.53%、山西汾酒 -3.73%）与老窖，抵消了腾讯与阿里的上涨

---

## 13. 已知偏差与边界

### 13.1 `holdings_source` 未按基金生效

见 §2：实际是全局投票决定源链顺序，单只基金的配置被忽略。
修复方向是让 `SyntheticValuator` 接收 `{name: source}` 映射，在 `evaluate()` 里按
`fund.holdings_source` 重排源链，而不是只接收一个固定列表。

### 13.2 `Quote.change_pct_hint` 采集了但没用

`tencent.py:90` 取 `parts[32]` 存入 `change_pct_hint`，`aggregate_holdings` 从不读取它
（一律用自算的 `change_pct`）。保留该字段是为了将来做"接口值 vs 自算值"的偏差监控。

### 13.3 新浪估值源（`SinaValuationSource`）的缺 GSZ 问题

`_fetch_one()` 只回填 `gszzl` 与 `nav`，**不填 `gsz`**（`sina.py:149`），
因此走新浪源时 `estimated_nav` 必须靠 `direct.py:161` 的反推逻辑补齐。
若 `nav` 也缺失，该条会被判为 `available` 但净值为 NULL —— 这是已知的边界情况。

### 13.4 `GZTIME` 校验只作用于 direct 路径

synthetic 路径没有等价校验：它用的是实时行情，天然是当日的；但**基准净值**可能是
上一交易日的（长假后甚至是 5 天前的）。`base_nav` 的日期记录在 `NavPoint.date`，
当前**没有落库**，需要的话可加一列。

### 13.5 direct 路径的 `confidence` 恒为 `high`

不看 `GZTIME` 距今多久、也不看接口是否延迟。若某基金接口估值长期不更新
（例如盘中停牌），仍会标 `high`。这是简化处理，下游若要求严格可结合 `GZTIME` 自行判断。
