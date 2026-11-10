# Research Output Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 修复实际投研输出中的行情错位、日期丢失、ETF 异常回测与非新闻污染，为人工研究提供可信依据，不添加自动交易。

**Architecture:** 在现有 provider → pipeline → report/tool 链路修复事实契约，保留历史兼容字段；ETF 在现有数据源链内重试，无法核实则阻止误导性指标。搜索准入继续复用现有日期与主体评分。

**Tech Stack:** Python、pandas、pytest、现有 React/Vite 客户端。

## Global Constraints

- 用户原话：只要策略结果、高价值资讯分析或投研建议，不用自动炒股。
- 保留工作区全部既有改动；不提交、推送、部署远端、发送通知、修改密钥或购买数据。
- 未知值不补零，不猜测复权因子，不为改善收益调参数。
- 可追溯：业务日期、来源、单位和质量边界必须到达报告及 Agent。
- Spec：本轮用户请求与 `local/audits/current-system-20261012/当前功能实跑报告.md` 的实测反例。

## Task 1: 行情与量价一致性

**Files:** `data_provider/tushare_fetcher.py`, `data_provider/akshare_fetcher.py`, `src/core/pipeline.py`, `src/analyzer.py`, `src/agent/tools/data_tools.py`, `src/agent/tools/analysis_tools.py`; tests: `tests/test_research_output_integrity.py`。

**Interfaces:** 保留 UnifiedRealtimeQuote 成交量“股”契约；`_enhance_context` 输出 today.pre_close、正确 yesterday 及 volume_comparison_basis；`_build_market_snapshot` 与 Agent 消费同一日期/单位。

- [x] 写回归：旧接口 volume=64,380,000 不得除100；盘中18.31/昨收18.05=1.44%，不得用10/8的18.24；盘中与完整昨日量不能直接断言缩量；技术工具返回K线日期。
```python
assert quote.volume == 64380000
assert snapshot['prev_close'] == '18.05'
assert snapshot['pct_chg'] == '1.44%'
assert snapshot['change_amount'] == '0.26'
```
- [x] `.venv\Scripts\python.exe -m pytest tests/test_research_output_integrity.py -q`：先观察上述断言失败。
- [x] provider 保持股；实时覆盖前将上一完整 today 移到 yesterday（仅日期早于报价日）；保存实时 pre_close；金额、涨幅、振幅同基准；不能跨日期混补实时与日线。
```python
volume=safe_int(row['volume'])  # legacy already returns shares
realtime_today['pre_close'] = getattr(realtime_quote, 'pre_close', None)
```
- [x] 同命令及既有 pipeline/report/quote 测试通过；逐文件备份代替提交。

## Task 2: 板块证据日期

**Files:** `src/market_analyzer.py`, `src/schemas/market_structure.py`, `src/services/market_hotspot_service.py`；同一回归文件。

**Interfaces:** RankedThemeItem 追加 as_of/provider/definition/coverage；板块表与 LLM 摘要保留行级日期；热点相位不能从单日涨幅推导加速。

- [x] 构造10/12生成、10/9板块数据；断言 prompt/table/hotspots 均含10/9且 phase=unknown，确认 RED。
```python
assert '2026-10-09' in analyzer._build_sector_block(overview)
assert hotspot['active_themes'][0]['as_of'] == '2026-10-09'
assert hotspot['active_themes'][0]['phase'] == 'unknown'
```
- [x] 排名摘要与表新增日期和来源；提示不同日期不可称今日主线，涨幅排名不代表持续性；热点保留元数据，不用请求日期替代来源日期。
- [x] 回归与 market structure/review tests GREEN。

## Task 3: ETF 数据验收

**Files:** `src/services/etf_rotation_service.py`, `tests/test_etf_rotation.py`。

**Interfaces:** LoadedPrices 追加质量拒绝记录；load_closes 对异常序列尝试后续已支持前复权源；build_report 独立阻止异常/不完整池的正式策略目标、历史收益与参数扫描。

- [x] 构造-80%序列及正常备用源；断言换源成功，全部失败明确报告；直接 build_report 异常序列不输出回测表现和目标权重，先 RED。
```python
assert report.target_weights == {}
assert '回测表现（' not in report.markdown
assert '数据验收未通过' in report.markdown
```
- [x] 把已存在25%异常检测用于准入，不做自动乘倍修正；每个可用前复权源最多一次经理链尝试；报告注明不完整候选池不能等同原策略。
- [x] ETF tests GREEN，真实默认池验证并记录仍缺失项。

## Task 4: 新闻准入与交付

**Files:** `src/search_service.py`, `tests/test_search_news_freshness.py`, `docs/research-output-integrity.md`, `docs/CHANGELOG.md`。

**Interfaces:** 复用 `_filter_ranked_news_for_context`；报价页不作为催化，不误删同域真实新闻；保留有日期、主体证据的公告，背景不得冒充公司事实。

- [x] TradingView `/symbols/SSE-601318/` 页面应过滤、`/news/...` 公司事件应保留，先 RED。
```python
assert [item.title for item in admitted.results] == ['中国平安发布业绩公告']
```
- [x] 精确按路径/页面标题识别纯行情页；不封整域名。提示词要求事实→影响机制→反证/待验证条件，不把行业背景当公司利好。
- [x] 新闻测试与受影响后端套件、py_compile、Web build GREEN；运行真实个股/大盘/ETF/搜索，只保存本地结果。
- [x] 最后独立只读 review，本轮差异对逐文件 baseline；处理重要问题并写验收文档。文档无英文对应文件，不改 README。

## Review Focus

检查历史日线与实时价格是否再混用；缺少/过期时间是否被伪装今日；报价缺字段和单位是否跨市场污染；ETF 缺池/安全资产/历史跳变是否仍输出可误解的完整指标；同域新闻是否误删；API 追加字段是否兼容。
