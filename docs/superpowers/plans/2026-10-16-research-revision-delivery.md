# Research Revision Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修好证据约束研判，并将 2026-10-15 收盘修订版安全投递至原 13 个飞书群。

**Architecture:** 模型选择程序已核验的研究信号和未来核验项，程序负责数字、关系与展示；新建显式修订身份，复用已有凭据、模型账本和单次 outbox，不重开原日任务。

**Tech Stack:** Python 3.11、SQLAlchemy/SQLite、pytest、React/TypeScript/Vitest，既有模型及飞书适配器。

**Spec:** `docs/superpowers/specs/2026-10-16-research-revision-delivery-design.md`，及已批准的 `2026-10-15-readable-research-brief-design.md`。

## Global Constraints

- 业务日明确为 2026-10-15；不改策略、排名、Top N、密钥或系统时钟。当前 13 个订阅，各 Top N=5。
- 不修改原 batch/publication/release/outbox/AutomationJob；新修订版使用独立 publication/release/outbox。
- 保留当前自动化授权、45 秒超时、零重试；默认每日 20 次模型预算。用户新增明确授权：仅北京时间 2026-10-16 临时提高到 40 次，其他日期自动恢复 20 次；旧无版本/v2/v3 冻结报告兼容。
- 无未经证据支持的数字、行业现状、榜单比较或收益承诺。历史来源使用原观测时间，不跨日补抓冒充原日。
- 仅根助手可进行真实模型/实库写入和最终发送；实现及审查代理只操作代码和临时测试数据库，不启停服务，不联网。
- 使用 apply_patch；禁止 Git commit/tag/push/reset/stash。保留当前脏分支及每任务局部快照；审查使用快照差异而非无效的空提交范围。
- 单实现者串行写代码，独立任务审查与最终集成审查；原有无关警告不得扩展为依赖升级。
- 本轮最多先做两次真实单份模型验收，随后每份修订版最多一次模型调用，遵守全局预算；同输入复用已有结果。全部预览检查后才发送。
- 不打印凭据、URL、原始模型响应或原始网络异常；只保存白名单诊断与规范化结果。

## Task 1: Close synthesis grounding gaps with selectable research signals

**Files:** Create `src/services/research_delivery/research_signals.py`, `tests/test_research_delivery_signals.py`; modify `research_brief.py`, `brief_rendering.py`, `explanation.py`, focused existing brief/explanation tests, topic docs and CHANGELOG.

**Interfaces:**

```python
def signal_catalog(brief_input: dict) -> dict: ...
# {id: {id, scope: summary|cross_signals|rationale|watch|invalidation,
#       code: str|None, text, kind, fact_refs, entity_refs, claim_domain}}
def signal_prompt_input(brief_input: dict) -> dict: ...
def validate_signal_explanation(text: str, brief_input: dict) -> dict: ...
# Returns the existing normalized paragraph schema plus signal_id on each segment.
```

Use expression_version=4 and a new prompt version only for newly built input. Existing validators remain for frozen earlier versions. Catalog is derived deterministically from frozen facts, not supplied model text. Model returns section lists of signal IDs (summary/cross_signals, each candidate rationale/watch/invalidation), exact candidate identity/order. A normalized segment may be revalidated only by exact equality with its catalog entry. Unknown/cross-candidate/wrong-role IDs are rejected locally; reordered/extra candidates reject the response. No arbitrary model prose is displayed in v4. The model still chooses which supported research tensions and follow-ups deserve emphasis; call this evidence-constrained selection, not open-ended autonomous research.

Catalog requirements: bounded per scope and candidate; 1 paragraph per public section; fixed factual basis plus genuinely conditional observations. Use actual positive/negative/zero distinctions, not generic claims. Prioritize existing growth/dividend/flow/financial tensions where evidence exists; otherwise describe only supported original strategy signals. Cross-domain tension needs both referenced domains; absent evidence is a follow-up, not a present fact. Market breadth relationships, industry mean-vs-breadth, cross-strategy overlap/ties/counts computed in code. Monetary/numeric displays remain program-owned and respect the existing three-metric public budget. Pure future watch text comes from bounded templates without assertions of unknown current states. Avoid duplicate prefixes such as 待观察：观察： in new output. Do not turn missing features into invented buy/sell advice.

- [x] RED: Add tests using frozen fixture values for the prior false claims:

```python
def test_unknown_or_tampered_signal_never_becomes_public(brief):
    result = validate_signal_explanation(selection_with_unknown_id(brief), brief)
    assert result['cross_signals'] == []
    assert 'unknown_signal' in ' '.join(result['diagnostics'])

def test_tied_overlap_is_program_owned(brief_with_overlap_5_5_3_2_2):
    catalog = signal_catalog(brief_with_overlap_5_5_3_2_2)
    joined = '\n'.join(item['text'] for item in catalog.values())
    assert '一只最高' not in joined
    # Assert exact tied subjects / program-derived count in the applicable entry.
```

Also test PE-only cannot select observed negative industry/positive flow signals, unknown-data pure future watch remains available, raw text cannot bypass normalized equality, missing core rationale gives fallback/partial diagnostics rather than synthesis success. Readable renderer must report adoption by role (`summary`, candidate rationale count, watch-only) in frozen detail. Old frozen v2/v3 replays remain identical.

- [x] GREEN: Implement catalog/validator in the new focused module and route newly built inputs through it. Prompt supplies selectable IDs/text, instructs exact JSON ID arrays and unchanged candidate order. Complete frozen input and prompt version remain in once-key. `ExplanationService`, formal preparation, renderer and saved replay all share the new path, not a CLI-only alternative.
- [x] Run `.venv/Scripts/python.exe -m pytest tests/test_research_delivery_signals.py tests/test_research_delivery_brief.py tests/test_research_delivery_explanation.py tests/test_research_delivery_formal.py tests/test_research_delivery_brief_preview.py -q` and py_compile changed Python files. Expected: pass with only existing warning. Document exact RED/GREEN and modifications; snapshot-relative diff; release write lock for review.
- [x] Root performs one real saved-data preview after independent review; inspect core summary and candidate rationales, not just status or paragraph count. Do not send here.

## Task 2: Add explicit revision prepare, inspection and one-attempt delivery

**Files:** Create `src/services/research_delivery/revision_publisher.py`, `scripts/revise_research_delivery.py`, `tests/test_research_delivery_revision.py`; extract shared saved-source verification/composition from `scripts/preview_research_brief.py` into `src/services/research_delivery/saved_research.py` if needed, preserving CLI aliases/contracts; update relevant frontend receipt type/presentation/tests, topic docs and CHANGELOG. User-authorized one-day budget addendum touches the existing repository budget gate, its tests and `.env.example` only as necessary.

**Interfaces:**

```python
class RevisionPublisher:
    def __init__(self, service, config, *, sender=None, clock=None): ...
    def prepare(self, original_publication_id, *, edition_id, evidence_dir=None,
                reference_manifest=None, background_manifest=None) -> dict: ...
    def inspect(self, edition_id) -> dict: ...
    def deliver(self, publication_id, *, edition_id, expected_manifest_hash) -> dict: ...
```

Preparation consumes verified saved batches/captures, not arbitrary preview JSON. New source snapshot embeds explicit original batch binding and added evidence; original rows stay byte-identical. Factor pure saved helpers out once, do not import CLI scripts into production. Preserve evidence path/hash/date/identity/coverage validation; use the real clock only as upper bound for original-date observations, not inject a fake current date into formal publishing.

Do not accept an arbitrary prebuilt context dictionary at this boundary. Construct it inside prepare from the verified explicit source locations and original day. Preserve original source JSON separately in the revision document binding. Frozen revision facts/envelope/brief must be derived from that validated augmented snapshot, not from caller-supplied preview fields. The evidence-only helpers are shared with the existing no-send CLI, whose default SQLite read-only semantics and restricted ledger-only model option remain unchanged.

CLI has `prepare`, `inspect`, `deliver`; DB/vault/day/edition/output paths explicit. Prepare/inspect never send, output only public manifests plus readable saved reports. Deliver requires explicit edition and expected manifest hash, checks exact complete prepared set before first network call. Edition UUID is operator-chosen once and reused; request identity `revision:{edition_id}:{subscription_id}` is unique and under 100 characters. Reject different original/source/content/targets with same identity. Do not reserve a new identity automatically on retry.

Freeze the complete expected edition scope before individual model calls: sorted original-publication/subscription/destination IDs, versions, Top N, model mode, business date and authorization epoch from the confirmed active original-sent set. Each new publication binds the identical scope and hash; preparation under the same edition with a different scope is rejected. The inspected manifest must match that frozen set exactly, then bind all new publication IDs, payload hashes and source digests. A missing/extra/changed member cannot produce a deliverable manifest; a single-record deliver validates membership in the complete approved manifest, not only its own hash. This avoids adding a separate coordination table for a single manual edition. CLI must use a strict existing database (mode=ro for inspect, mode=rw for prepare/deliver) and no schema initializer or recovery-on-read method. No new DB file on a typo.

- [x] RED: temporary SQLite tests cover original-sent-only, same ID reentry, conflicting original/hash, concurrent send once, revocation/config rotation, original integrity changes, expiry, payload mutations, and unknown outcome non-retry:

```python
first = publisher.deliver(pub_id, edition_id=edition, expected_manifest_hash=manifest_hash)
again = publisher.deliver(pub_id, edition_id=edition, expected_manifest_hash=manifest_hash)
assert first['status'] == again['status'] == 'sent'
assert len(sender_calls) == 1
assert original_rows_after == original_rows_before
```

- [x] GREEN: Validate original publication/document/source hashes, original release/outbox payload/hash and status sent, matching subscription/category/day and current source strategy version. Freeze current enabled total authorization epoch, enabled destination/subscription versions, credentials, Top N and LLM mode. Use real time and a 60-minute new release validity; never alter original expiry or formal window. Check configuration twice around model generation and again immediately before durable send claim. No daily AutomationJob updates; revision uses own durable outbox claim without stealing automation lease.

Validate the exact original formal receipt chain (request ID, unique publication link, release, batch, source, destination/version). Publication.status may remain prepared after a successful send; use outbox.status=sent. Reuse old-format frozen_payload compatibility, never rerender old text with the new formatter. Original coverage/row validations and every original evidence byte hash, metadata, path, business day and timestamp are required in addition to added-context validation. Resolve the original day independently from real current time; do not call FormalPublisher._window/_batch/_validate_source with a fake clock. Same-ID reentry must validate identity/source/configuration conflicts before deciding no model is needed. Expose only allowlisted metadata in CLI manifest; no credential refs or raw source bindings.
- [x] Reuse `formal_envelope`, `build_brief_input`, `ExplanationService`, `render_brief`, `frozen_payload`, `send_once`, repository atomic and vault. New docs are frozen before sending; payload ≤16KiB, keyword in visible text. New title includes 修订版 and original business date without debug/manual-trigger prose. Preserve names/order/Top N. Resolve credentials in memory only. Commit sending/attempt_count=1 before network; status sent/rejected/delivery_unknown terminal. Existing abandoned sending recovery applies, no format fallback resend.
- [x] UI: teach exact receipt association to recognize kind=revision separately, display 修订版研究报告, not a formal original. No group-creation/settings redesign. Add focused component/type tests, lint and build; no dependency installation needed.
- [x] Root content acceptance found an announcement readability defect: five announcement records for four issuers were followed by four anonymous identical 解读/待观察 blocks. Fix only new v4 announcement formatting in `brief_rendering.py`: aggregate exact identical generic paragraphs and name their associated issuers once; retain distinct supported issuer interpretations with explicit issuer labels. No duplicate generic fallback per issuer, no change to records/order/Top N/links or legacy frozen formats. Preserve truthful role-adoption accounting after grouping. Add focused rendering/legacy replay regression and document the adjustment.
- [x] Root content acceptance corrections, all for new explicitly version-marked shared inputs only (old frozen v4/v3/v2/unversioned unchanged): label `volatility_20d_pct` as 近20日收益率年化波动率 because the existing calculation is std*sqrt(252)*100; no value change. Preserve an operator-friendly one-line caveat for snapshot-only scoring and unavailable strategy-critical interval/technical/theme metrics (not raw audit logs), including momentum_quality, oversold_reversal, capital_heat and balanced_alpha. For shrink_pullback, when displayed same-window volume-ratio facts do not show contraction, explicitly say rule entry does not confirm the contraction pattern; no re-ranking or new condition in strategy code. Test real-shaped missing fields and opposing ratio values. Shared formal/revision/preview generation must receive the frozen presentation marker and complete once-key; legacy frozen replay remains identical.
- [x] User-approved temporary budget: add one optional operator environment value `RESEARCH_DELIVERY_LLM_BUDGET_OVERRIDE=YYYY-MM-DD:40`. The existing atomic reserve gate allows 40 only when the validated ISO date exactly matches the attempt's Shanghai business day; malformed, absent, expired or different-date values use the unchanged default 20. Do not delete/relabel ledger attempts, change once-keys, retries/timeouts, or add settings UI/schema. Unit tests cover matching 40th/41st boundary, next-day 20th/21st boundary, malformed values and cached-result reuse. Update `.env.example` and topic docs. Only root sets the actual local value `2026-10-16:40` after review; workers never access `.env`. Root verifies effect and automatic expiry before final service restart.
- [x] Verify full affected backend tests, focused frontend tests/lint/build, compile and CLI `--help` without model/network. Write report + diff and release lock. Independent review must check new authorization/identity/historical source/transport boundaries.

## Final acceptance and authorized delivery (root only)

- [x] Snapshot original tables read-only. Check real clock, current 13 enabled targets, versions and remaining daily model budget without exposing secrets.
- [x] Freeze one edition UUID. Prepare all 13 reports from 2026-10-15 saved batches plus verified original-date reference/background captures. Review summary/rationale adoption, missing real data, actual date, unchanged ranking/names/metrics and all card bytes. Do not regenerate if failed; inspect once-key result.
- [x] Fresh final integration review before sending; fix important issues once then scoped re-review. Do not count an offline pass as real acceptance.
- [x] Build/restart project service only when no running job, health-check. Explicit deliver using reviewed manifest digest, one attempt per target. Persist and inspect actual per-group outcomes, never promise exactly-once remote delivery under lost response.
- [x] Compare original snapshots unchanged; count new revision rows/attempts and report sent/rejected/unknown honestly. Keep evidence, no Git commit or remote deployment. Rollback code does not remove ledger or retract messages.

## Plan self-review

Task 1 produces a shared new frozen selection contract consumed by Task 2 through existing renderer/service APIs. Task 2 owns saved-source reuse and UI receipt integration. All financial facts and actual sends remain separate from unit-test fixtures. New manual authority is explicit, scoped to the confirmed previous close, and never achieved by weakening the automatic publisher.
