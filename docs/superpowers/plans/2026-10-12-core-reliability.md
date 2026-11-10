# 阶跃核心链路可靠性修复 Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan inline, task-by-task. Steps use checkbox syntax for tracking. This task does not authorize git commits, pushes, purchases, live model tests, schedulers or trading.

**Goal:** 修复 Tushare 请求路径、TickFlow 免费配额边界、选股结构化输出边界和缺行情/汇率的估值真实性。

**Architecture:** 复用现有 Tushare HTTP 客户端和取数管理器；在 TickFlow 适配器内集中控制拆批与请求预算。保留排序的覆盖率/候选白名单和现有因子降级；以追加完整性标记保持持仓数值 API 兼容，未知数据不参与有效估值和风险结论。

**Tech Stack:** 项目 .venv、pytest/unittest、FastAPI/Pydantic、React/TypeScript、Vitest。

**Execution:** 2026-10-12 本轮五项任务已完成；逐项结果以 local/audits/core-reliability-20261012/progress.md 为准，验收见 local/audits/2026-10-12-core-reliability-acceptance.md。保留未提交差异，不部署或合并。后续真实模型/供应商验收不属于本轮完成声明。

## Global Constraints

- 用户确认了前一轮第 1 阶段的四项修复；后续宏观/BTC、部署、模拟收益验证不在本轮范围。
- 保留已有未提交 UI/品牌改动；修复分支为 codex/core-reliability-20261012。
- 不改用户 .env、不输出 Key、不运行收费模型或真实股票推荐任务、不写用户交易数据库。
- 后端测试只用假传输和临时数据库；前端验证沿用现有 UI 风格。
- 遵循 AGENTS.md：不提交/推送；改可见行为时同步专题文档和扁平 CHANGELOG。
- 跨币种缺汇率不再使用 1:1；已有数值字段仅兼容为已知部分小计，新增 valuation_complete/limitations 让 UI、Agent 和风险消费者拒绝完整估值结论。
- TDD、verification-before-completion、using-git-worktrees 辅助技能当前不可用；执行相同的失败复现→修复→回归流程。用当前工作副本和本地分支保留既有开发成果，不另建遗漏脏文件的工作树，不删除未提交验证记录。

## Task 1: Tushare 请求协议一致

**Files:** data_provider/tushare_fetcher.py；src/services/screening/snapshot.py；src/services/screening/daily.py；tests/test_core_reliability.py。

**Interfaces:** 输入 Token 与现有 TUSHARE_API_URL/TUSHARE_HTTP_URL 别名；输出现有 DataFrame 和归一化字段，不改变筛选日线复权规则。

- [ ] 假 requests.post 返回 daily/daily_basic/stock_basic JSON，实际调用 `_fetch_tushare()` 与 `_fetch_daily_tushare()`；断言 URL 是原地址、请求体 api_name 正确、没有 SDK 自动增加的 ts_type_name。
- [ ] Run `.venv\Scripts\python.exe -m pytest tests/test_core_reliability.py -k tushare -q`。Expected：旧路径断言失败。
- [ ] 提供 `create_tushare_http_client(token, timeout=30)`，按现有 URL 别名优先级创建 `_TushareHttpClient`；两条筛选取数路径直接使用它，去掉 SDK 私有属性修改。
- [ ] Run 同一测试及已有 Tushare/筛选日线测试。Expected：全部通过，无真实请求。

## Task 2: TickFlow 拆批与非阻塞限流

**Files:** data_provider/tickflow_fetcher.py；src/config.py；src/core/config_registry.py；data_provider/base.py；.env.example；tests/test_core_reliability.py；tests/test_tickflow_fetcher.py。

**Interfaces:** 保留已保存 batch_size；新增 max_symbols_per_request 和 requests_per_minute，保守默认分别为 5 和 10，付费用户可按实际权限显式提高。不得覆盖用户已存配置。

- [ ] 用假 K 线/报价资源记录请求：11 标的、batch_size=100，断言每次至多 5 标的。模拟 monotonic，跨适配器同 Key 第 11 次请求应限流，60 秒后恢复，缓存命中不扣预算。
- [ ] Run `.venv\Scripts\python.exe -m pytest tests/test_core_reliability.py -k tickflow -q`。Expected：拆批/限流失败。
- [ ] 加上限拆批与按 Key 共享的进程内请求窗口，超额立即失败并走已有备用，不睡眠阻塞；配置贯穿所有管理器初始化和设置元数据。避免用无限重试耗尽额度。
- [ ] Run TickFlow 适配器、路由、预取和复盘备用测试。Expected：全部通过。

## Task 3: 结构化排序安全与明确降级

**Files:** src/services/screening/ranker.py；tests/test_screening_ranker.py；tests/test_core_reliability.py；现有选股 UI 的诊断消费点（仅必要时）。

**Interfaces:** 保留 LLMRankingResult、候选白名单、覆盖率、调用方 max_tokens 和模型回退。没有真实复现旧供应商响应时，不宣称供应商兼容问题全部解决。

- [ ] 复现 content 的完整前置 think 包装包含草稿 JSON，最终 JSON 在包装外；最终结果必须采用最终 JSON。finish_reason=length 的响应必须明确降级，不接受被截断候选的部分结果。
- [ ] Run 定向新测试。Expected：旧实现选中草稿/接受截断返回。
- [ ] 复用 strip_leading_think_wrapper；传输阶段识别截断/无最终文本，记录结构化失败原因；不增加模型预算、不把思考内容当成最终输出。核查前端已显示因子回退，否则添加状态文案。
- [ ] Run 现有 ranker、pipeline、history/API、候选覆盖与注入安全回归。Expected：全部通过。

## Task 4: 缺行情/汇率的估值与风险可信性

**Files:** src/services/portfolio_service.py；src/services/portfolio_risk_service.py；api/v1/schemas/portfolio.py；apps/dsa-web/src/types/portfolio.ts；apps/dsa-web/src/pages/PortfolioPage.tsx；tests/test_core_reliability.py；tests/test_portfolio_service.py；apps/dsa-web/src/pages/__tests__/PortfolioPage.test.tsx。

**Interfaces:** 追加 valuation_complete、缺口 limitations；保持账本和数据库数值列兼容。明确数值为已知部分小计，UI 完整总额显示 `--`。不为缺行情创造收益、止损或集中度结论。

- [ ] 临时数据库/假仓库：缺价格、缺 FX、已知价格与缺失混合、现金跨币种、非有限输入；断言完整性 false、具体缺口可见，缺 FX 不返回原金额，风险不输出“正常”或 100% 损失。
- [ ] Run 定向新测试。Expected：旧实现完整性缺失/1:1/假止损断言失败。
- [ ] 缺价格只保留数量成本；缺 FX 返回不可换算的零小计与 missing_fx 状态，不进行假转换；沿账户和跨账户汇总传播缺口。消费者判断完整性，前端隐藏未知总额/盈亏，风险返回 available=false/reason 并跳过缺报价止损；回撤排除不完整日期。
- [ ] Run portfolio service/API/risk/alerts/Agent tests 与 PortfolioPage Vitest。Expected：全部通过，正常完整账户保持原结果。

## Task 5: 文档、综合验证与独立审查

**Files:** docs/core-reliability.md；docs/CHANGELOG.md；local/audits/core-reliability-20261012/（本地验证日志与基线备份）。

- [ ] 记录缺口含义、配额调整、未在线验证范围、兼容性和逐文件回滚方法；不修改无关文档。
- [ ] Run 受影响后端测试、py_compile、前端定向测试、lint/build。Expected：变更范围通过；已有无关失败单独列出。
- [ ] 依 executing-plans/requesting-code-review 的要求进行一次独立代码审查，仅审本轮差异。Important/Critical 补失败回归后一次修复，不自动提交。
- [ ] 保留验证记录与既有用户改动。交付：修复点、测试计数、未验证项和风险；线上/收费模型验收单独确认。

## Review Focus

- SDK/HTTP URL 别名、超时、请求载荷及数据单位是否保持一致。
- TickFlow 多实例并发是否绕过预算，所有资源请求是否受控，限流是否阻塞回退，付费覆盖是否可配置。
- 截断 JSON/思考草稿是否仍被当作完成排序；覆盖率与候选白名单有无放宽。
- 缺 FX/报价在现金、已实现收益、持仓、跨账户、回撤、告警和 Agent 的全链路是否误导；旧 API 数值兼容是否清楚。
- 预存 UI 修改是否保留；新的状态是否中英文一致，未知值是否误显示 0。
