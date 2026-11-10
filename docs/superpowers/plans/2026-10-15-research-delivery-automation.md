# 可信盘后计算与飞书多群自动化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task. Preserve the existing dirty checkout; no commits or remote deployment are authorized by this plan.

**Goal:** 接通已配置的 13 类群订阅：当日采集 → 证据/覆盖校验 → 原有策略计算或资讯整合 → 可选 LLM 解释 → 冻结正文 → 分群发送 → 持久化日程与回执。用户已明确授权开发、启用现有订阅及运行一次今天的正式推送。

**Architecture:** 独立于旧单机器人/个股自动分析任务的应用内 worker，随 FastAPI 生命周期启动；默认关闭，数据库保存授权和版本。可信 collector 仅在服务端运行，旧历史草稿继续禁止发布。共享当日市场快照及日线缓存，复用现有十个 YAML 策略，不修改选股规则。正式发送复用单次 Feishu 发送器及持久 outbox，不自动重发未知送达。

**Tech Stack:** Python 3.11 / pandas / SQLAlchemy SQLite / FastAPI / existing OpenAI-compatible explanation adapter / React TypeScript / Vitest.

## Global Constraints

- 不下单，不保证收益；不使用历史结果冒充今天；不暴露 key、Webhook、个人持仓数量或成本。
- 保留管理员、同源和 HTTPS/本机凭证保护；不因 Edge 扩展改写 Origin 而放宽安全边界。
- 一个实现者写代码，其他席位可做只读调查与评审；独立临时数据库和模拟 provider/sender 完成测试后才进入真实发送。
- 未授权 Git 提交/推送；现有未提交成果不能覆盖。缺少 worktree 技能且现有功能大量未提交，本轮保留当前 feature checkout，以基线文件副本和改动范围审查替代 commit-range 审查。
- 应用内日程使用北京时间，数据最早 17:05 获取，订阅使用已保存的各自时间；当日 22:30 截止。非交易日不发 A 股策略，休眠/关机不能执行，恢复后只补当日窗口内未发送任务。
- 手动“运行今天”仅绕过订阅的计划时刻，不绕过交易日、收盘、日期/数据/版本/凭证校验；与日程共享幂等键。
- 允许有限覆盖研究，但必须披露独立股票池、数据覆盖、合法排除、技术候选上限与未评估数量；未知缺口不能改成 0 或“无风险”。行情覆盖低于 95% 阻断，正常不适用指标按明确原因排除并披露，不缩分母冒充全市场。
- 数据以已配置的 Tushare 为主；采用 a-share-data-fusion 的原始证据、来源、日期、单位与缺口纪律，不新建旁路推送或购买新服务。
- 纯离线 preview 的 publish_allowed=False 不改。正式批次只能由服务器 collector/producer 写入，不接收浏览器的已验证布尔值/股票名单/消息正文。
- 新表旁挂旧表，create_all 可兼容已有数据库；外部网络调用不得放进 SQLite 写事务。
- 开发说明为内部中文文档；本次不镜像翻译。更新 CHANGELOG 的 Unreleased 平铺条目。

## Shared Contracts

`ProducerResult`（新 `live_contracts.py`）：category_id、business_date(ISO)、state(ready/degraded/blocked/skipped)、strategy_version、result(dict)、coverage(dict)、evidence(list)、warnings(list)、blockers(list)。全部 JSON 安全，原始响应保存在数据目录 evidence store，以 SHA256 标识，不存 token/请求鉴权头。错误只用固定安全代码。

`LiveCollector(config, evidence_dir, *, client=None, now=None, request_interval=1.0)`：`collect(strategy_ids, business_date, progress=None) -> dict[str, ProducerResult]`。先采集与验证当日数据；各策略复用 frozen preview 的真实计算。参考名称以当日 stock_basic 的 observed-at/effective-as-of 独立证据处理，不能伪造交易日期；历史业务日不支持名称重建则阻断。

`produce_auxiliary(category_id, *, business_date, config, evidence_dir, collector_context=None) -> ProducerResult`：ETF、雷达、公告的有界无发送适配器。来源不足时返回清楚的 blocked，不发布占位榜单。公告范围使用现有配置的公开股票代码，不暴露账户事实；空范围明确阻断。

可信持久化模块：ResearchBatch / PublicationRelease / AutomationControl / AutomationJob 新表；唯一键业务日+订阅 ID（配置改动不会自动制造同日二次推送）。job 包含状态、阶段、错误码、数据日期、批次/投递 ID、更新时间；工作租约与 run_id 防止并发和崩溃后旧工作者继续发送。

`FormalPublisher(service, config)`：`store_batch(ProducerResult)`（仅内部）；`prepare(subscription_id, batch_id)` 冻结双版本/凭据引用/排名/风险，使用现有 ExplanationService；`deliver(publication_id)` 再验全局授权/订阅/目的地/窗口/内容哈希，durable sending 后执行 send_once。文本超过平台限制则阻断，不暗中截股票或自动换渠道。

`ResearchAutomation(service, config, *, collector_factory=..., auxiliary=..., publisher=..., clock=...)`：`status()` / `configure(version, enabled, subscription_ids)` / `request_run(request_id)` / `tick()` / `start()` / `stop()`。异步触发返回状态，HTTP 请求不等待大批采集。每次采集共享市场输入，策略失败彼此隔离；仅数据采集明确失败可有限重试（最多 3 次、间隔至少 10 分钟），Feishu unknown 不重发。

## Task 1: Live A-share collector and existing strategy computation

**Files:** add `src/services/research_delivery/live_contracts.py`, `live_collector.py`, `evidence_store.py`; narrowly update `evidence.py` reference-metadata validation if needed; add `tests/test_research_delivery_live_collector.py`.

- [x] RED: fixed-day calendar/universe, names with reference evidence, stale/partial/denied inputs, factor conflict, hands→shares, no fabricated PE, reused candidate histories, exact ten-strategy dispatch.
- [x] Implement endpoint capture with bounded timeout/rate, safe hashes, strict same-date daily/daily_basic joins and independent stock_basic pool; ≥95% response coverage guard and explicit per-code exclusions.
- [x] Warmup >=61 observations; corporate-action forward adjustment with matching factors and fixed end date; reject duplicates/future/stale and validate exchange-session gaps or clearly disclose bounded observed-bar history without claiming contiguous completeness.
- [x] Invoke existing preview/compute-only rules; separate sampling policy from strategy logic. No env mutation, LLM, Feishu, live network in tests.
- [x] GREEN: focused new suite plus existing readiness/preview/Tushare/compute-only suites.

## Task 2: Auxiliary producers

**Files:** new `src/services/research_delivery/auxiliary_producers.py`; optional narrow `src/announcement_source.py` date-anchor support; tests.

- [x] RED: ETF actual last quote and warmup, radar source timestamp, issuer/date-filtered announcement scope, partial failures and empty scopes.
- [x] Reuse ETF load_closes/build_report without run_etf_rotation side effects; reuse hotspot service without stale date relabeling; reuse announcement fetch with explicit date/scope.
- [x] Capture sources and quality; each produces the shared ProducerResult. No hidden LLM/send/notification initialization.
- [x] GREEN: focused producer and existing related suites.

## Task 3: Trusted batches and formal publishing

**Files:** new `automation_models.py`, `formal_publisher.py`, `render_formal.py`; repository table registration; necessary ExplanationService prompt support; tests.

- [x] RED: untrusted history rejected, frozen batch/versions/hash/window, duplicate and concurrent requests, unknown never retried, destination rotation/pause, LLM failure and ranking manipulation, oversized payload, old database initialization.
- [x] Persist trusted source and release alongside existing models, no destructive schema migration.
- [x] Render ordinary dated strategy reports (not test/manual notices), source/coverage/missing/risk retained, LLM explanation cannot alter picks or order.
- [x] Commit sending before network, one send only; platform acknowledgment is acceptance, not read receipt.
- [x] GREEN: new formal suite plus existing outbox/explanation/security/manual-flow tests.

## Task 4: Persistent worker, application lifecycle, admin API

**Files:** new `automation.py`; narrow `api/app.py`, research_delivery endpoint/schema, capabilities/categories; tests.

- [x] RED: disabled default, same-day dedupe manual vs scheduled, multiple workers/lease fencing, restart, timezone/holiday/closing/cutoff, config-version changes, partial category failure, stop/pause pending delivery.
- [x] Persist authorization and subscription activation; application owns worker independent of legacy SCHEDULE_ENABLED/no-notify flags. No startup immediate catch-up outside safe window.
- [x] Add GET `/automation`, PUT `/automation` optimistic version, POST `/automation/run` request_id; no arbitrary payload/target injection.
- [x] Status includes each configured subscription's schedule, last stage/error, date/coverage, delivery state; heartbeat and running status distinguish enabled from alive.
- [x] GREEN: focused API/lifecycle/worker integration tests with fake clocks/providers and temporary DB.

## Task 5: UI and operating documentation

**Files:** researchDelivery API/types, new AutomationPanel component, existing ResearchDeliveryPanel labels; frontend tests; `docs/feishu-research-delivery.md`, `docs/CHANGELOG.md`.

- [x] RED: status load, activation/pause, run-today pending + refresh, failures/blocked data visible, preserve independent group saving and legacy drafts.
- [x] Clear controls: 自动推送总开关、运行今天、分群计划时间/状态/原因；do not imply saving a new group automatically authorizes sends.
- [x] GREEN: Vitest affected tests, TypeScript and production build; same-origin local browser verification and screenshot outside tracked repository.

## Task 6: Independent review and authorized live acceptance

- [x] Independent spec/quality review of each task; fix critical/important issues; final integration review after all code.
- [x] Back up existing SQLite with SQLite backup API and preserve vault unchanged; no secrets in output.
- [x] Restart local application; enable the existing 13 configured subscriptions using the normal admin/runtime API; verify worker heartbeat and next schedules.
- [x] Run today's jobs via the same automatic path; query actual calendar/date/source coverage, real strategy counts, LLM status and Feishu business acknowledgments. Record blocked producers honestly; do not substitute stale outputs.
- [x] Publish concise actual results: enabled state, per-category sent/blocked reason, next timing, host uptime requirement. Do not claim complete if any required function remains unimplemented.

## Task 7: Live-acceptance model compatibility correction

Actual native acceptance found repeated `invalid_model_output` with exactly 1800 completion tokens. This is new runtime evidence, not a residual final-review finding. Diagnose one bounded request through the existing budget ledger without any Feishu send, preserving raw-response privacy. If verified, apply a narrow research-explanation-specific DeepSeek default thinking correction, preserve explicit configuration and all ranking/schema/risk gates, test SDK wire behavior and independently review. Do not raise token/call caps or resend today's immutable published reports. Restart only when native jobs have no in-flight work; no pause/re-enable epoch churn.

- [x] Safe bounded diagnostic separates truncation from invalid structured content.
- [x] Minimal compatibility fix and isolated regression tests, independent review.
- [x] Real explanation validation, no model-output bypass or repeated group send.
