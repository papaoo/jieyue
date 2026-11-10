# 本地免费行情源验证（阶段一）

核验日期：2026-10-09。范围仅为现有 `DataFetcherManager` 的一个公开 ETF 日线样本，不是全市场覆盖测试、实时行情认证或策略收益验证。

以下集成阻碍是首次探测时的代码快照，保留用于追溯；历史接口随后完成的兼容修复见文末“历史接口质量契约修复后状态”。在线样本证据未被离线测试替换。

## 实测结论

无需行情 API key 或 LLM key，现有腾讯适配器成功读取 `510300`（沪深 300 ETF）日线。基础价格、成交量和日期检查通过；成交额全部缺失，实际复权分支未被现有适配器暴露。首期可用于带明确质量标签的 ETF 日线展示，尚不足以宣称高可信实时投研、跨资产排序或盈利能力。

| 项目 | 实测结果 |
| --- | --- |
| 调用入口 | `DataFetcherManager(fetchers=[TencentFetcher()]).get_daily_data(...)`，探测子类仅观察原始适配器行数 |
| 来源 | `TencentFetcher`；腾讯公开 `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get` |
| key / 付费接口 / 模型 | 无；仅显式注册腾讯源，不走默认多源、模型、推送或调度 |
| 抓取时间 | `2026-10-09T14:37:43+00:00`（北京时间 22:37:43） |
| 请求窗口 | 2026-08-10 至 2026-10-09 |
| 返回行数 | 39；适配器窗口过滤后原始行数同为 39，不代表 HTTP 原响应总条数 |
| 最早 / 最新 bar | 2026-08-10 / 2026-10-09 |
| fetcher 用时 / probe 用时 | 516 ms / 0.906 秒（不含子进程解释器与导入开销） |
| 必要字段缺失 | `date/open/high/low/close/volume` 均为 0 |
| 缺失成交额 | `amount` 39/39；必须保持 `null`，不能补成 0 或估算值 |
| 日期检查 | 升序；重复日期 0 |
| 价格 / 成交量检查 | 非正价格 0；负成交量 0；OHLC 关系异常 0 |
| 最新 bar 完整性 | 日期等于抓取当天；未独立核验交易日历及供应商最终收盘修订状态 |

最后一条实测 bar（公开样本，非用户自选股）：

```json
{
  "date": "2026-10-09",
  "open": 4.369,
  "high": 4.4,
  "low": 4.309,
  "close": 4.385,
  "volume": 1276733100.0,
  "amount": null,
  "pct_chg": -0.0911369332
}
```

这是一次数据读取证据，不是对价格准确性、可交易性、ETF未来表现的保证。未进行第二源交叉核对、全交易日连续性核对或复权因子核对。

## 可复用探测命令

在仓库根目录的 PowerShell 中运行：

```powershell
& '.\.venv\Scripts\python.exe' -B '.\scripts\probe-market-data.py' --symbol 510300 --days 30 --timeout 30
```

默认不传参数同样读取 `510300`；`--days` 限 1–60，适配器请求窗口取两倍日历日，并不保证返回恰好指定数量的交易日。`--timeout` 限 1–60 秒，默认 30 秒，限制整个隔离子进程；适配器单次 HTTP 自带 8 秒超时。成功且核心字段/基础检查通过退出 0；读取失败、超时或核心质量失败退出 1，保留 JSON 失败证据，绝不返回 mock 成功。

安全边界：

- 子进程仅保留必要的操作系统、现有代理/证书设置；不继承应用模型、推送、用户列表或付费数据 key 配置。
- `ENV_FILE` 指向系统空设备，避免加载项目 `.env`；关闭远程股票索引更新。不读取鉴权凭据文件。
- 仅调用公开 GET 日线端点，不导入应用启动入口，不建立 DB，不保存行情仓库或运行报告，不启动 Web/定时任务，不通知、不交易。
- 只输出稳定错误类型，不输出异常原文、代理地址、完整环境、密钥或用户自选股。
- 诊断记录只在该子进程内存中；这次 probe **不会**自动把 Web 数据中心的状态变成已实测成功。

已完成验证：真实在线命令退出 0；脚本 `--help` 正常；通过 AST 语法检查，不生成编译缓存。补跑腾讯适配器的 6 个定向离线测试（前复权响应解析、缺失 amount、空历史、成交量换算及科创板口径），全部通过，14 项未运行。测试使用 mock GET，不新增在线请求，禁用 pytest 缓存/第三方插件自动加载，`ENV_FILE=NUL`。出现一个现有 Starlette/httpx 弃用警告；未修改依赖、未运行整个项目测试套件。

## 字段、单位与复权边界

返回列为 `date, open, high, low, close, volume, amount, pct_chg, ma5, ma10, ma20, volume_ratio`。

| 字段 | 来源/单位 | 注意事项 |
| --- | --- | --- |
| OHLC | 腾讯；A 股/ETF 适配器口径为人民币元/股或份 | 未与交易所或第二源校对 |
| volume | 腾讯原始非科创板手数由适配器乘 100，规范为股/份 | 单位依赖原适配器实现，本次未交叉核验 ETF 原始成交量口径 |
| amount | 此样本全缺失 | 不使用其支持资金流或成交额信号；单位未验证 |
| pct_chg | 规范化层从相邻返回 close 计算，百分数 | 第一条因缺少上一条数据填为 0，并非真实当日收益证据 |
| MA5/10/20 | `BaseFetcher` 计算，价格单位 | `min_periods=1`，早期均线未完成完整窗口；当前实现舍入为两位小数 |
| volume_ratio | 日成交量 / 前 5 日均量 | 不是交易软件的同一时刻分时量比；首条缺值填为 1 |
| 复权 | 请求参数 `qfq`（前复权） | `_extract_kline_rows` 优先 `qfqday`，但可回退 `day`；DataFrame 无实际复权标记，所以本次 `reportedAdjustment=null` |

关键实现位置：`data_provider/tencent_fetcher.py:43`（请求）、`:96`（规范化）、`:243`（成交量换算）、`:257`（`qfqday/day` 回退）；`data_provider/base.py:467`（统一入口）、`:546`（清洗）、`:576`（指标计算）、`:1917`（管理器路由）。

## 首期前端可接的现有 API

以下为当前源码审查结论，**本次未通过 HTTP 调用验收**；依现有 Web 鉴权和部署设置访问，不开放未经鉴权的外网服务。

| 用途 | 现有入口 | 可复用边界 |
| --- | --- | --- |
| 数据中心 / 来源质量状态 | `GET /api/v1/data/overview`（`/data/capabilities` 为同结构别名） | 返回 provider 配置、能力、dataset quality、priority、warnings；`unknown`/未探测不能当成功。优先对接，勿把 markets × datasets 当作所有组合均支持 |
| ETF / 股票日线图 | `GET /api/v1/stocks/510300/history?period=daily&days=30` | 已走 DataFetcherManager；当前仅 daily 真正实现。需要补来源、复权、币种、时区、bar 完整性与字段缺失元数据 |
| 单资产研究档案 | `GET /api/v1/stocks/510300/profile?history_days=60` | 已有分块质量状态，可借鉴局部失败不拖垮全页；仍是股票身份模型，不是完整多资产合约 |
| 已落库资讯 | `GET /api/v1/intelligence/items?page=1&page_size=20` | 读取已保存资讯，空列表就是暂无数据；不暗示会自动抓取。抓取类 POST 不纳入本轮只读探测 |
| 筛选模块能力 | `GET /api/v1/screening/status`、`GET /api/v1/screening/strategies` | 先读规则/配置状态，不主动启动选股 POST；规则筛选不代表经过盈利验证的量化策略 |
| Web 健康 | `GET /api/v1/health` | 验证应用进程，不等于行情源、LLM 或宏观源可用 |

对应源码：`api/v1/endpoints/data.py:39`、`:57`；`api/v1/endpoints/stocks.py:414`、`:517`；`api/v1/endpoints/intelligence.py:154`；`api/v1/endpoints/screening.py:113`、`:118`；`api/v1/endpoints/health.py:21`。

## 需要修复或后续验证的集成阻碍

1. **历史 API 丢失证据元数据。** `src/services/stock_service.py:120` 接到 `(df, source)`，但 `:147` 返回字典没有 source；`StockHistoryResponse` 同样缺乏 source/as-of/currency/adjustment/quality。失败捕获返回空数组，也不能区分“无历史”和“源失效”。首期应扩展兼容的质量字段，不把空数据画成零价。
2. **缺失数值序列化需要专测。** `stock_service.py:140–142` 用真值判断而非缺失判断，NaN 可被保留、合法 0 可被变成 None。这是代码层风险，本次未证明当前 FastAPI HTTP 响应是否报错；接图前用缺失 amount 样本做接口回归，并统一输出 JSON null。
3. **实际复权状态不透明。** 腾讯适配器允许 `day` 回退但不声明；跨源回测/轮动必须明确 requested 与 observed adjustment，未知状态不能混用为可比前复权序列。
4. **无独立宏观/加密资产读取 API。** 当前 v1 endpoints 没有宏观时序或 BTC OHLCV 路由；FXMacroData 位于 Agent 工具层，不应为首页事实数据启动 LLM。股票代码路由/校验不应直接复用 `BTC-USD`；黄金 ETF `518880` 也不能冒充现货黄金。
5. **当前免费源成功不代表商业授权或 SLA。** 仅证实此机、此时、此 ETF 的日线读取得到有效字段；全市场、限流、停牌、复权修订、海外源与商业再分发权限仍须分别核验。

下一步最小测试矩阵：第二个 A 股 ETF（如黄金 ETF，仅测试资产类型）、一只美股公开日线、BTC 专用合约与免费源（需新增适配）、一个不经 LLM 的宏观时序；每项只跑小样本，保留源/采集时间/币种/复权/最新 bar/缺失/延迟，并覆盖源失败与陈旧数据界面状态。未经用户授权不启用付费源、自动分析、外发或交易。

本阶段变更仅为探测脚本与本记录；未修改 provider、`.env`、前端或业务逻辑。数据核验遵循 `a-share-data-fusion` 的来源、时间、缺失字段与未知状态显式记录原则。

## 历史接口质量契约修复后状态（2026-10-09）

经后续集成授权，修复限于 `StockService.get_history_data`、`StockHistoryResponse`、历史 GET 的响应传递与定向测试。此前探测脚本、腾讯 provider、默认源顺序、`.env`、实时 quote、模型、推送及调度均未改变。

历史响应保留原有 `stock_code/stock_name/period/data`，追加向后兼容字段：

- `source`：继承本次 manager 实际返回的数据源；未知为 null，不凭配置推测。
- `fetched_at`：请求处理完成时的 UTC 时间，**不是行情观察时间、最新 bar 时间或刷新供应商保证**。
- `status`：`available` 仅表示获取到可序列化、必要价格有效且可选字段齐全的条目；可选字段缺失或无效价格行丢弃为 `partial`；上游空结果为 `empty`；读取失败为 `unavailable`。不把失败、空结果解释为市场正常。
- `warnings`：固定包含 `daily-not-realtime`、`adjustment-unknown`、`freshness-not-verified`；另明确标注可选字段缺失、丢弃的无效 OHLC 行、空结果或源不可用。
- `adjustment`：仍为 null。此修复只改证据传递，不替 provider 证明实际前复权口径。

可选 `volume/amount/change_percent` 的非有限数或无法转换的数值输出 JSON null；合法 0 保留。必需 OHLC 的缺失/NaN/Inf 不补成 0，该行跳过并给 partial 警告；全部行无效时仍保留 partial 与原因，不声称“无行情且正常”。原有 weekly/monthly 未实现时的 HTTP 422 行为不变。

确定性测试只替换仓库/外部行情边界，实际执行服务、Pydantic 与 FastAPI ASGI 路由/JSON 序列化，覆盖 NaN、正负 Inf、None、零值、各 OHLC 缺失、全部行无效、源异常、空结果及旧响应形状。测试里的数据是显式构造的 fixture，绝不作为免费源在线成功证据。运行命令：

```powershell
$env:ENV_FILE = 'NUL'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& '.\.venv\Scripts\python.exe' -B -m pytest -q -p no:cacheprovider '.\tests\test_stock_history_quality.py'
```

验证结果：24 项定向测试全部通过，4 个变更 Python 文件的 `py_compile` 通过，生产文件 `git diff --check` 无错误；保留一个现有 Starlette/httpx 弃用警告。此隔离环境未安装 flake8，本轮未安装额外工具。

这项修复解决历史 GET 的来源丢失和数值边界问题。首次修复时仍发现 `/stocks/{code}/profile` 的旧聚合逻辑未继承历史质量字段；该发现与后续修复分开记录如下，不将历史 `fresh` 标签当作行情新鲜认证。

## 研究档案下游质量传递与静态契约验证（2026-10-09）

经后续集成授权，仅扩展 `StockProfileService._history_block`、`StockProfileHistoryBlock` 和相关现有测试。档案的历史块现继承 `source/fetched_at/adjustment`，并将历史 `warnings` 原样去重传入 `limitations`。上游 `partial` 或包含 `freshness-not-verified` 时状态保持 `partial`；旧结果没有完整质量元数据也保持 `partial`，加 `history-freshness-not-verified`。空结果、源不可用或无有效历史条目仍为 `unavailable`，保留原因，不升级为健康。

新增元数据都是 Optional 字段，旧字段和旧响应仍可解析。现有 Web 前端没有股票 profile 历史类型或消费者，无需为新增 Optional 字段修改旧界面。静态 `docs/architecture/api_spec.json` 只同步历史块三个新增字段，并补录现有 history GET 路径及 `StockHistoryResponse`；未用运行时 OpenAPI 全量覆盖静态文档。

```powershell
$env:ENV_FILE = 'NUL'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:STOCK_INDEX_REMOTE_UPDATE_ENABLED = 'false'
& '.\.venv\Scripts\python.exe' -B -m pytest -q -p no:cacheprovider '.\tests\test_stock_history_quality.py' '.\tests\test_stock_profile_api.py'
```

结果：62 项全部通过（历史质量 25 项、完整档案 37 项，37.50 秒），包含 history/profile 的静态 OpenAPI 与实际应用契约精确比较、下游未知新鲜度/部分数据/旧形状/空失败状态及真实 profile HTTP JSON 测试；6 个变更 Python 文件 `py_compile` 通过，相关生产/测试/静态契约 `git diff --check` 无错误。保留两个测试环境既有警告：Starlette/httpx 弃用及测试客户端类未收集警告。未执行模型、推送、调度或付费接口。

仍未完成第二源校验、交易日历/供应商修订验证、直接 BTC/现货黄金接入或完整投研盈利验证；实时 quote 与其他档案块的证据边界不属于本次历史契约修复范围。

### 正价与 OHLC 包络补充验证

首次质量修复处理了非有限值，随后进一步排除有限但无效的价格：OHLC 必须全部大于 0，并满足 `high >= max(open, close, low)`、`low <= min(open, close, high)`。不满足的行跳过，保持 `partial` 和 `invalid-ohlc-rows-skipped` 原因；不填 0，不将全部无效行当成正常空市场。合法正价平盘保留，成交量/成交额/涨跌幅的合法 0 不受此价格约束影响。

五项实际 HTTP 包络反例在补充修复前均失败（旧状态错误地为 `available`），修复后连同每个 OHLC 的 0/负数/负 Inf 与平盘测试通过。使用上述同一隔离命令重跑两个测试文件：**80 项全部通过**（历史质量 43 项、完整档案 37 项，27.12 秒），仅保留上述两个既有测试警告。仍为确定性 fixture 测试，不新增在线行情成功或盈利证据。
