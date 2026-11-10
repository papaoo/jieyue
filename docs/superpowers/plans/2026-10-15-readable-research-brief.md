# Readable Research Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐正式推送的名称、当日行业及候选背景，以可读卡片展示有证据的综合研判。

**Architecture:** 继续使用既有采集、冻结 batch、FormalPublisher 和单次 outbox。数据上下文在发布前冻结；策略规则不变，解释与渲染使用同一研究包。历史 text 报告兼容，新卡片与预览共享内容。

**Tech Stack:** Python 3.11 / pandas / SQLAlchemy / Pydantic / pytest；React / TypeScript / Vitest；现有 Tushare、Tencent、新闻和公告适配器。

**Spec:** `docs/superpowers/specs/2026-10-15-readable-research-brief-design.md`

## Global Constraints

- 不改各策略的筛选公式、分数或排序，不新增市场产品，不下单，不自动重发今天已发送的消息，不新增收费服务或修改用户密钥。
- 保留持久化 outbox、同日订阅幂等、原凭据管理与同源管理权限；未知发送结果不得换格式重发。
- 名称、日期、数值必须有来源；旧数据不能冒充当日行情，财报按披露日与报告期处理。
- 未经明确确认，不执行 `git commit`、`git tag`、`git push`。保留现有 dirty checkout；使用 apply_patch 编辑。
- 不打印 `.env`、Webhook、密钥、cookie 或原始异常。测试用项目 `.venv/Scripts/python.exe`，不得操作真实 outbox。
- 新格式先本地预览；真实网络验证只做少量数据查询和有预算的模型预览，不发送飞书。
- 单实现者写代码；独立审查可并行。每任务写报告、独立 spec/quality gate；不要求用户再次确认中间任务。

## Shared interfaces

`ProducerResult` 顶层结构不变。在 `result` 内追加 `research_context`：

```python
context = {
    "version": "research-context-v1",
    "business_date": "2026-10-15",
    "instruments": {},  # code -> {code, name, asset_type, industry, source, observed_at, status}
    "industries": {},   # existing industry_facts price result; explicit definition/as_of/source
    "market_breadth": {},
    "background": {},   # code -> independent dated provider blocks, not scoring inputs
    "cross_strategy": {},  # code -> list of same-day strategy IDs
    "compared_strategies": [],
    "statuses": {},     # domain -> available/partial/not_requested/failed/stale
}
```

纯计算与持久化证据阅读必须不加载配置、不开网络、不发送。新增模块用明确参数接收配置/依赖。

## Task 1: Names and dated shared market facts

**Files:**
- Create `src/services/research_delivery/market_context.py`
- Modify `src/services/research_delivery/live_collector.py`
- Modify `src/services/research_delivery/auxiliary_producers.py`
- Modify only name-lookup error logging in `data_provider/base.py`, `data_provider/tencent_fetcher.py` to respect existing safe-exception context (newly exercised path).
- Test `tests/test_research_delivery_market_context.py`
- Update focused existing `tests/test_research_delivery_auxiliary.py`, `tests/test_research_delivery_live_collector.py` only when new correct contract changes expectations.

**Interfaces:**
- Produce `build_market_context(reference, daily, *, business_date, fetched_at, source)` -> shared context dict. Reuse `data_provider.tushare_fact_normalizers.industry_facts`.
- Produce `load_market_context(stock_batches, *, business_date, evidence_dir, now)` -> context dict from verified stored `stock_basic`/`daily`; do not trust arbitrary unverified context.
- Both names and industry facts carry actual provenance. Limit per-result instrument dictionary to relevant candidates where possible; reference reader supports announcement codes.
- ETF result retains `execution_assumptions.lookback_days`, adds resolved `items[].name` and named `research_context.instruments`; unknown names remain explicit, never guessed.

- [ ] RED: add deterministic tests for current industry aggregation, old date rejected, evidence path/hash mismatch, announcement names from exact reference, ETF resolver output and explicit unknown.

```python
def test_current_industry_context_has_human_names():
    reference = pd.DataFrame([{"ts_code": "601166.SH", "name": "兴业银行", "industry": "银行"}])
    daily = pd.DataFrame([{"ts_code": "601166.SH", "trade_date": "20261015", "pct_chg": 0.6, "amount": 10.0}])
    result = build_market_context(reference, daily, business_date="2026-10-15",
                                  fetched_at="2026-10-15T18:00:00+08:00", source="tushare")
    assert result["instruments"]["601166"]["name"] == "兴业银行"
    assert result["industries"]["source"] == "computed_from_tushare"
    assert result["industries"]["as_of"] == "2026-10-15"
```

- [ ] Implement: add `industry` to LiveCollector stock_basic fields, preserve names and derive market context from raw daily/reference before filtered universe. Attach as `result.research_context` without changing picks.
- [ ] Implement: radar consumes verified shared industry context; do not reuse ETF-only `_manager` for board capability. If no industry data, use properly scoped existing provider with current-date validation, or retain specific missing status. Do not stamp undated provider output as current. Save sanitized source/rejection metadata.
- [ ] Implement: ETF uses existing name resolution; announcement names come from current exact-code reference. Store resolved labels in source before freeze, not by network I/O during rendering.
- [ ] Name lookup wraps existing safe-exception context; raw provider exceptions/stacks must not appear in that context. Add a sentinel regression; do not globally suppress normal diagnostics.

```python
result = dict(existing_result)
result["research_context"] = context
# Required invariant: result["picks"] == existing_result["picks"]
```

- [ ] GREEN: `.venv/Scripts/python.exe -m pytest tests/test_research_delivery_market_context.py tests/test_research_delivery_auxiliary.py tests/test_research_delivery_live_collector.py -q`
- [ ] Self-review, report file and independent review; no Git commit.

## Task 2: Bounded shared candidate research context

**Files:**
- Create `src/services/research_delivery/research_context.py`
- Modify `src/services/research_delivery/automation.py`
- Modify `src/services/research_delivery/formal_publisher.py` only if needed to expose frozen context; coordinate with later Task 3.
- Test `tests/test_research_delivery_research_context.py`
- Extend `tests/test_research_delivery_automation.py` for optional-enrichment failures and no duplicate requests.

**Interfaces:**
- `ResearchContextEnricher(config, *, fundamental_fetcher=None, news_fetcher=None, announcement_fetcher=None, clock=None)`; instance retained by worker for same-day request sharing.
- `enrich(result, *, related_results, top_n_by_strategy, progress=None)` -> new ProducerResult. Preserve picks/order/scores, existing evidence; append normalized evidence where new facts are used.
- Consumes Task 1 context and produces `background`, `cross_strategy`, `compared_strategies`, `statuses`. Only known same-day completed results count as cross-signals.
- Maximum union 100 unique Top-N symbols per day; shared cache keys include code and business date. Per-call budget 90 seconds total, per-symbol fundamental budget 8 seconds; news/announcements share residual budget. Existing adapter timeout/retry limits remain bounded. Budget exhaustion preserves partial facts and records status, not whole-report failure.
- Rehydrate requested-code/cached background state from same-day frozen related results after restart; do not rewrite them. The 100-code ceiling is not a claim that all codes will complete within 90 seconds.
- Existing callables: `TushareFundamentalAdapter(query).fetch(code, budget_seconds=..., as_of=business_date, on_block=...)`; explicit runtime `create_tushare_http_client` or existing collector query. `SearchService(...explicit configured keys...).search_topic_news_bounded(topic, max_results=5, focus_keywords=[name, code], timeout_seconds=...)`. News stays a dated company-related lead, not issuer-verified financial fact.
- The adapter also requests market-wide tables near its tail: reuse same-day normalized query results across candidates, or explicitly skip redundant whole-market enrichment when Task 1 already supplies those facts; do not re-fetch whole-market tables once per candidate. Capture partial results without claiming skipped domains are provider failures.
- `fetch_company_announcements(code, days=3, limit=10, as_of=...)` lacks residual timeout: execute through a bounded process wrapper modeled on existing search service deadline mechanism, reserving cleanup time; do not use detached timeout threads. Injected test callables can be synchronous with controlled monotonic time. Never swallow `progress` lease/authorization exceptions.

- [ ] RED: tests show eligible Top-N only, same stock reused across strategy enrichment, no scores/rank mutation, exact issuer/date match, financial prior report accepted only if already disclosed, expired quote rejected, provider failure yields usable base results.

```python
before = deepcopy(source.result["picks"])
first = enricher.enrich(source, related_results=[source], top_n_by_strategy={source.category_id: 5})
assert first.result["picks"] == before
assert first.result["research_context"]["business_date"] == source.business_date
second = enricher.enrich(source, related_results=[source], top_n_by_strategy={source.category_id: 5})
assert fake_fundamentals.call_count == len({p["code"] for p in before[:5]})
```

- [ ] Implement actual reuse of existing fundamental adapter and search/announcement services, with safe date/entity normalization. Use existing snapshot PE/PB as known basic valuation; do not equate incomplete full-risk context with absent PE/PB. Keep true domain gaps internally.
- [ ] Integrate before immutable `store_batch`; initialize from explicit runtime config, not global env inside pure helpers. In-memory results include all completed current collection results so cross-strategy comparisons aren't loop-order dependent. Cached frozen batches remain immutable. Failed optional enrichment must not block or duplicate base publication.

```python
enriched = enricher.enrich(result, related_results=related_results,
                          top_n_by_strategy=top_n_by_strategy, progress=self._progress)
self._producer_result(publisher, row, enriched, cache)
```

- [ ] GREEN: `.venv/Scripts/python.exe -m pytest tests/test_research_delivery_research_context.py tests/test_research_delivery_automation.py -q`
- [ ] Self-review/report/independent review. Record actual interface consumed by Task 3.

## Task 3: Evidence-backed brief and frozen Feishu card

**Files:**
- Create `src/services/research_delivery/research_brief.py` (pure context-to-facts + structured model contract validation)
- Create `src/services/research_delivery/brief_rendering.py` (brief text/card from one frozen document)
- Modify `src/services/research_delivery/render_formal.py`, `explanation.py`, `formal_publisher.py`
- Test `tests/test_research_delivery_brief.py`; extend formal/explanation tests.

**Interfaces:**
- `build_brief_input(envelope, detail, source)` -> versioned, public fact dictionary and strategy context. Every fact has identity/domain/subject/source/date, quantities have units. Include strategy definition and ETF lookback.
- `validate_brief_explanation(text, brief_input)` -> validated summary/cross_signals/candidate rationale/watch/invalidation + safe section diagnostic codes. Candidate identities/order mismatch is global rejection; invalid local sections fall back locally.
- `render_brief(title, envelope, detail, llm, brief_input)` -> `{format_version, rendered_content, card_payload}` using no I/O. Preserve all Top N and material specific risks.
- Existing `validate_explanation`/historical drafts remain backwards compatible; explicit version selects new format. Model once-key includes prompt/format version and entire frozen input.
- Integration seams: both `formal_envelope` and `ExplanationService` have explicit allowlists, and FormalPublisher revalidates model output; extend all three deliberately for the new formal-only schema. Do not change historical `research-explain-v3` defaults. Existing announcement Top N slices records before issuer de-duplication; preserve it. Keyword requirements must exist in visible card text, not only its detached summary. Delivery reads frozen content only.
- Suggested segment schema: `{kind: fact|inference|condition, text, fact_refs, claim_domain, entity_refs}` within report summary/cross_signals and candidate rationale/watch/invalidation. Program-owned fact domains/entities/dates are authoritative. Numbers use fact-reference placeholders; future observation conditions may discuss currently unknown domains without asserting present facts. Validate sections independently; domain compatibility plus targeted cross-domain checks is not a proof of arbitrary prose truth, and docs must not claim it is.

- [ ] RED: test formatting, names, ETF percentages/window, unsupported claims, no numbers invented, source/date mismatch, prompt injection data, guarantee rejection, valid conditional interpretation accepted, paragraph-level fallback, unchanged order.

```python
brief = render_brief(title, envelope, detail, llm, brief_input)
assert "\n\n" in brief["rendered_content"]
assert "必需字段校验" not in brief["rendered_content"]
assert "发布窗口" not in brief["rendered_content"]
assert brief["card_payload"]["msg_type"] == "interactive"
assert "代码（代码）" not in brief["rendered_content"]
```

- [ ] Implement report-level synthesis and candidate interpretation from explicit domains. Model can give research lean/observation/contradiction, cannot edit ranked stocks or assert unsourced capital/earnings facts. Prefer referenced facts and program-filled numbers; retain genuinely conditional watch statements.
- [ ] Implement domain-specific brief metrics, names on ETF weights and explanations, grouped specific gaps; audit details stay in document detail. Core judgment first, visually separated candidate blocks, optional cross-signals, next-watch. No localhost detail URL.
- [ ] Upgrade both prepare and deliver payload integrity paths to same versioned renderer/validator. New `interactive` card and historical `text` support; keep exactly-one send and existing hashing/lease/config checks. Check <=16KiB serialized payload before preparing; Top N=20 must fit by compact presentation without silently dropping candidates or specific material risk.

```python
# New document fields are additive; old documents select legacy payload.
payload = document.get("card_payload") or {"msg_type": "text", "content": {"text": document["rendered_content"]}}
# Delivery verifies the complete frozen content and approved versioned payload before send_once.
```

- [ ] GREEN: `.venv/Scripts/python.exe -m pytest tests/test_research_delivery_brief.py tests/test_research_delivery_explanation.py tests/test_research_delivery_formal.py tests/test_research_delivery_outbox.py tests/test_research_delivery_automation.py -q`
- [ ] Self-review/report/independent review. Do not send real messages.

## Task 4: User-visible dates, details, docs and acceptance

**Files:**
- Modify `apps/dsa-web/src/components/report/MarketReviewReportView.tsx`
- Modify `apps/dsa-web/src/components/settings/research-delivery/ResearchPreview.tsx`
- Modify relevant researchDelivery types/API schemas only for additive public brief fields; preserve old client format.
- Add optional source/asOf/definition/coverage fields to existing `SectorRankings` types as needed; history payload is camel-cased at the API client boundary. Existing report content styles live in `apps/dsa-web/src/components/settings/research-delivery/research-delivery.css`; small readability changes belong there, not a parallel theme.
- Create/update focused component tests under existing `__tests__` directories.
- Update `docs/feishu-research-delivery.md`, `docs/CHANGELOG.md`.
- Create `scripts/preview_research_brief.py` (explicit no-send, readonly saved batch replay; optional bounded enrich/model via flags; artifacts outside repository product sources).

**Interfaces:**
- UI displays readable frozen brief text/cards; audit sections collapsed. Sector panels show their `as_of` plus historical date when old, with equal-weight definition when applicable.
- Preview command reads SQL using mode=ro and writes only explicitly selected preview artifacts. It never prepares/releases/sends publications or alters automation state. Real model calls use existing daily attempt ledger/budget, only with explicit flag; source input hash and prompt version determine reuse.
- Read-only replay selects `research_automation_batches` directly and verifies result JSON hash plus category/date/version bindings. Do not instantiate normal `ResearchDeliveryRepository` (constructor creates schema) or call `deliveries()` (outbox recovery writes). The explicit model option may use a narrow ledger-only adapter reusing existing `reserve_llm/finish_llm`, no alternate budget or publisher calls.
- Historical augmentation composes verified saved tables using existing capture checks and `build_market_context`; do not relax current-day guards in `load_market_context` or `ResearchContextEnricher`. Preserve business date, captured observation time and current generation time separately. After rollover do not fetch new fundamental/news data and relabel it as a prior-day observation.
- Optional offline HTML artifact must escape all content, have no remote scripts/assets, and prominently identify itself as a local preview (not a sent publication). Screenshot that artifact separately from the real React component checks. Verify existing outbox row states/attempts as well as count remain unchanged.

- [ ] RED: component tests for historical date disclosure, local detail link omitted, old/new report display; preview command no-send test and missing-name case.

```tsx
expect(screen.getByText(/2026-10-12/)).toBeInTheDocument();
const detailLink = screen.queryByRole('link', { name: /完整报告/ });
expect(detailLink?.getAttribute('href') ?? '').not.toMatch(/127\.0\.0\.1|localhost/);
```

- [ ] Implement UI/date/details/docs and preview CLI using Task 1–3 functions, not duplicate formatting. CLI output prominently identifies preview locally, never changes historical sent records.
- [ ] GREEN: run `tests/test_research_delivery_*.py` once as the final cross-task backend regression (including auth/setup/credentials/API/manual-flow/outbox and new preview CLI tests); focused Vitest, `npm run lint`, `npm run build` under `apps/dsa-web`. Use existing installed dependencies, no install unless a required dependency is actually missing.
- [ ] Replay saved 2026-10-15 stock/ETF/radar/announcement inputs. After date rollover preserve data date and observation time separately; do not bypass production date gates. Query only the missing reference mapping and seven ETF names if needed; optionally a few candidate fundamentals/news. At most two additional real model validation calls in this acceptance, using existing remaining budget. No Feishu sends.
- Early capture already completed at 2026-10-15 23:14 CST: `.superpowers/sdd/2026-10-15-readable-research-brief/acceptance/reference-capture/manifest.json` points to the verified new reference mapping and seven names. Reuse it for preview augmentation only, with provenance and actual observed time; do not repeat those requests or mutate old production batches. The capture has 5576 stocks, 5575 non-null industry values, seven resolved ETFs.
- One bounded candidate capture also completed before rollover at 23:57–23:58 CST: `acceptance/background-capture/manifest.json` in the same workspace links original batch ID/hash and normalized `candidate_background` capture for 601166. Optional verified background-manifest input should reuse that capture for saved-input preview; verify SHA/metadata/date/entity and original source binding, do not trust mutable manifest background directly. Growth/dividend/financial/flow facts are present; zero qualifying news/announcement rows is not a provider failure or proof none exist.
- [ ] Preview at desktop/mobile widths using existing browser tools, save visual evidence outside tracked sources. Compare names/metrics/date/ranking with original frozen input and check zero new formal outbox records.
- [ ] Independent task gate and whole-change review, fix findings, restart local service only after tests and no active job; validate health and unchanged delivery count. Final user report links previews, states remaining true source gaps, no promise of profitability.

## Progress

- [x] Task 1 complete and reviewed.
- [x] Task 2 complete and reviewed.
- [x] Task 3 implemented and independently reviewed; actual AI adoption remains unaccepted (see acceptance note below).
- [x] Task 4 implementation, saved-data replay and UI checks completed and reviewed.
- [x] Final change review, four local previews and safe local service restart/health completed; original sends unchanged.
- [ ] Actual configured-model synthesis usability: v3 shape/count compatibility passes, but real synthesis remains unaccepted. Core summary/rationales were rejected; adopted cross/condition prose exposed evidence-scope issues. Do not claim the complete AI research feature is finished.

Acceptance note (2026-10-16): new stock/ETF/radar/announcement previews from saved2026-10-15data preserve names/order/ranks/metrics andfit16KiB. Secondrealcall finishednormally(stop,2022outputtokens) butallsevensections failedarray/lengthcontract,zeroAIparagraphsadopted. Rawreplynotretained, exactshapeunknown. Two-callbudgetexhausted; no thirdcall/noFeishuresend. Data enrichment/readable deterministic fallback handed off locally; AI synthesis remains a known incomplete requirement. Evidence and recoverable baselines are retained under `.superpowers/sdd/2026-10-15-readable-research-brief/acceptance/` and its parentworkspace; noGitcommit/push/remote deployment.

Plan self-review: all spec requirements mapped to Tasks 1–4; no strategy-rank changes; shared context keys and version fixed above. Task 4 owns end-to-end evidence rather than claiming unit-test success is live acceptance. UI visual companion is unnecessary for implementation: the structure was already approved.

Follow-up acceptance (2026-10-16, new user request to resend after completion): expression/prompt v3 fixes bounded section normalization and safe shape diagnostics; 183 focused tests pass. One new real preview completed with stop/1303 output tokens and 6 adopted paragraphs, but these are only cross_signals plus five watch paragraphs. Summary and all five rationales remain rejected. Independent review found an unsupported single-highest overlap claim (actual counts 5/5/3/2/2) and conditions importing industry weakness while citing only PE. Do not equate status=succeeded or adopted>0 with synthesis acceptance. New code has not been restarted into the local service; no Feishu resend occurred. Original outbox/publication/release/batch hashes and automation authorization remain unchanged. Follow-up evidence: `.superpowers/research-delivery-20261016-resend/`. Explicit historical revision delivery also requires its own approved path; the existing current-day formal publisher must not be bypassed.
