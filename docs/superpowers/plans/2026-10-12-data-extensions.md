# Remaining Data Coverage Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task. Preserve the existing dirty worktree; do not commit or push.

**Goal:** Fill usable, verifiable dividend, holder-change, multi-session Dragon-Tiger and industry context, and repair news provider failover.

**Architecture:** Extend the existing Tushare adapter with dated, bounded queries and pure normalization helpers. Reuse the configured 2000-point token; keep original provider facts separate from derived aggregates. Propagate evidence into the existing screening prompts, never synthesize missing data.

**Tech Stack:** Python, pandas, pytest, existing HTTP client and search providers.

## Global Constraints

- No purchases, new secrets, trading, notifications, deployment, commits, or pushes.
- All completed blocks survive optional-query failure; no failed query becomes a zero/negative event finding.
- API additions only; preserve UI and user changes. Backups: `local/audits/data-extensions-baseline/`.
- Dates, currency, source, scope and unknown fields accompany facts. Holder snapshots are not actual trades; industry aggregates are not vendor sector indices.

## Task 1: Disclosed company facts

Files: extend `data_provider/tushare_fundamental_adapter.py`; create `data_provider/tushare_fact_normalizers.py`, `tests/test_tushare_extended_facts.py`.

Interface: `dividend_facts(frame, cutoff)` returns dated events, implemented trailing-year cash per share and explicit coverage; `holder_facts(frame)` returns two-period comparable-holder deltas with institution classification and exclusions. Empty newly-listed/partial holdings remain scoped, never full-market institutions.

- [x] Write and run fixtures for duplicate dividend proposal/implementation, future announcements, missing cash, new/disappeared holders, and custodians.
- [x] Implement per-share (not per-ten-share) normalization, preserve provider `dv_ttm`, add one bounded `dividend` query; compare same named holder across adjacent disclosed periods, exclude custodians from financial-institution totals.
- [x] Run `.venv/Scripts/python.exe -m pytest tests/test_tushare_extended_facts.py tests/test_data_completeness.py -q` and require pass.

## Task 2: Industry aggregate and multi-session coverage

Files: same adapter/helpers; `data_provider/tushare_fetcher.py` for per-fetcher bounded shared-table cache.

Interface: adapter `fetch` retains existing blocks; market-wide `stock_basic`, `daily`, `moneyflow` yield dated sector rankings with population/observation counts. `trade_cal` plus five dated `top_list` tables yield unique session counts and checked/missing sessions.

- [x] Write tests for incomplete industry coverage, mixed session rejection, duplicate leaderboard reasons, failed one-day response and cached frame isolation.
- [x] Reuse market-wide tables across candidate stocks; cache only nonempty tables, scope to fetcher/token and bounded TTL/entries. Preserve deadline for each HTTP request.
- [x] Sort separate industry price and money-flow rankings; expose equal-weight price and large+extra-large net inflow definitions. If observations missing, set partial.
- [x] Count unique Dragon-Tiger dates, not duplicate reasons; never report confirmed absence across unchecked dates. Use exchange calendar (not weekdays); before nightly publication do not count same-day empty table as no event.
- [x] Run focused tests and bounded three-stock real-data verification.

## Task 3: News and evidence consumers

Files: `src/search_service.py`, `src/services/screening/ranker.py`, `src/services/screening_service.py`, associated tests.

- [x] Write provider-exception fallback test (first raises, second valid dated company news).
- [x] Continue to next news provider on exception without exposing server errors; preserve date/relevance filters and retain truthful empty state if no valid article.
- [x] Include bounded dividend, holder and multi-session/industry facts in ranking context, preserving definitions and partial coverage; add consumer regression.
- [x] Run full affected suite, Python compilation and frontend build; bounded live smoke, restart local service and verify health.

## Execution ledger

- Probed existing credentials: dividend, holders, trade calendar and top_list work. Tushare DC/THS industry-flow APIs deny permission. Use transparent own aggregation, not a paid upgrade.
- Ruling: retain current feature branch and dirty files; local backups substitute for unrequested commits. No separate worktree because it would omit the existing integrated modifications.
- Ruling: execute inline as continuation of the user's repair request; unavailable TDD/verification subskills are replaced by explicit red/green tests and recorded real commands. The plan itself serves as the persistent ledger.

## Review focus

Look for future-data leakage; silently incomplete market tables; aggregation unit errors; proposal/implementation double counting; incorrectly equating holder changes with net buying; empty failed news/day requests treated as confirmed negatives; missing propagation into actual consumers; credential exposure.

## Completion ledger

- Task 1: complete. Initial normalizer tests failed on missing implementation, then passed. Company fixtures now verify dates, revisions, units, zero/unknown and custodians.
- Task 2: complete. 110 observed industries; 5558/5559 mapped. Last symbol source industry is actually null. Both mock cache isolation and real shared-table reuse verified.
- Task 3: core complete. News exception reproduced then fixed. Agent native path, nested history extraction, standard prompt and notification contracts verified. Legacy-only manager regression caught by expanded suite and fixed by native capability gating.
- Final: fixed 5 reviewer findings with red/green regressions (revisions, missing publication, mixed daily dates, ttm_event_count compatibility, standard prompt scope). Final 26-file suite: 1084 passed / 21 skipped / 0 failed. Python compilation and frontend build passed.
- Ruling: allocate 90% (previously75%) of unchanged stage budget to native facts, because cold-start five-session data otherwise lost to the fixed sub-budget; less guaranteed time for public-web fallback, all unused native time remains available.
- Ruling: extend consumer fix to `src/utils/data_processing.py`, `src/analyzer.py`, `src/notification.py` and legacy sector endpoint, because otherwise new data would be lost or mislabelled. Additive fields only; regression suite covers changed contracts.
- Remaining limits, performance/live run details and rollback rationale are recorded in `docs/data-completeness-repair.md` (second-round final acceptance). No known reviewer minor findings deferred; fundamental data limitations remain explicit, not counted as full coverage.
- Restart: fixed UTF-8 decoding of process-state JSON in `scripts/local-web.ps1` after Windows PowerShell misread the Chinese workspace path. Existing PID/executable/command/start-time checks remain unchanged. Verified Status→Stop→Start with the same old PowerShell entry point, then health OK and homepage200 at13:40:16. PID28112, loopback listener22708; no user data/logs removed.
