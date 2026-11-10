# Tushare 快照证据接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this approved W1 slice. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正生产 Tushare 筛选快照的估值和日期口径，并提供保持声明股票池的离线证据诊断。

**Architecture:** 复用现有 snapshot collector、缓存和 research_delivery 验收器。行情与每日指标按代码一对一合并，各保留原业务日期；严格研究路径保留无行情行，普通手动路径保留有效价格过滤。新增纯诊断适配器不创建网络客户端、不加载环境配置、不授权发布。

**Tech Stack:** Python 3.11、Pandas、现有 Tushare HTTP client、pytest；项目 `.venv`，不增加依赖。

## Global Constraints

- 不下单、不承诺收益；缺数不伪造零值；未知业务日期不包装成今日数据。
- 保留现有手动分析/单群通知兼容；未经明确确认不 commit、tag、push，不部署、不启用群推送。
- 当前工作区已有大量未提交变更；任务结束只报告本任务差异，禁止重置、批量覆盖或擅自整理它们。
- 默认禁用真实投递；保存配置不发送；首次上线和规则变更后人工审核。
- 本批不读取 `.env`、真实 token；测试使用明确的离线桩，不调用收费接口、模型或通知。
- 不给旧缓存补写证据；不把查询参数、采集时间、整表最大日期当作字段业务日期；不以普通 PE 回填缺失的 TTM。
- `stock_basic` 没有业务日期，名称/股票状态验证仍必须显示未完成；不得给名称硬贴行情日期来放行预览。
- 原始证据持久化、独立日历/股票池验证、停牌/亏损不适用凭据、复权历史、ETF、调度和投递仍属于后续 W1—W6；本批不宣称完成它们。

---

## Task 1: 修正共用 Tushare 快照及缓存契约

**Files:** Modify `src/services/screening/snapshot.py`; Create `tests/test_tushare_snapshot_evidence.py`. Only adjust existing tests if their raw-provider schema is obsolete; keep coverage of existing behavior.

**Interfaces:** Keep `_fetch_tushare()` and `_prepare_tushare_snapshot(daily, daily_basic, stock_basic)` existing calls compatible. Add keyword-only `preserve_invalid_rows: bool = False` to `_prepare_tushare_snapshot` and `_normalize`; Task 2 uses it for evidence diagnostics. Retain `trade_date` for quote date; add `daily_basic_trade_date` for the basics date. Add attrs `units_profile="cn_equity_cny_v1"`, `pe_ratio_basis="ttm"`, `field_trade_date_columns`; the map excludes `name` because no dated reference evidence exists. These describe transformations, not authentication or publication approval.

- [x] Step 1: TDD regression file. Fixtures use two codes, quote dates T/T-1, basic dates T-1/T, `pe` different from `pe_ttm`, thousand-yuan amount and ten-thousand-yuan caps. Cover absent `pe_ttm` => missing, not static fallback; null and negative TTM; missing basic row/date; duplicate/null code and duplicate columns rejected before many-to-many merges; symbol conflicts rejected; original DataFrames/attrs unchanged; invalid prices retained only with the explicit strict option; all non-Tushare default behavior unchanged. Assert source request asks for `trade_date` and `pe_ttm` using a fake HTTP client.

```python
assert output.loc[0, "pe_ratio"] == 12  # raw pe=7, pe_ttm=12
assert output.loc[0, "amount"] == 2_000_000  # raw 2000 千元
assert output.loc[0, "total_mv"] == 300_000_000  # raw 30000 万元
assert output.loc[0, "trade_date"] == "20261013"
assert output.loc[0, "daily_basic_trade_date"] == "20261012"
assert output.attrs["field_trade_date_columns"]["pe_ratio"] == "daily_basic_trade_date"
assert "name" not in output.attrs["field_trade_date_columns"]
```

- [x] Step 2: Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_tushare_snapshot_evidence.py --tb=short`; record semantic RED before implementation.
- [x] Step 3: Update production request and normalization contract:

```python
# daily_basic request fields; never rename pe to pe_ttm
fields = "ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv"
# Work on copies; limit raw table columns to each endpoint's owned fields.
# Validate non-null unique ts_code and unique column names before joins.
# Rename daily_basic.trade_date BEFORE merge to prevent trade_date_x/y.
# Merge with validate="one_to_one"; canonical code comes from ts_code.
# Existing symbol must match canonical code, never silently change identity.
# Map pe_ratio from pe_ttm only; missing stays NaN for the gate.
# Coerce units once; reject booleans as numeric evidence rather than 1/0.
# _normalize defaults to dropping invalid prices, optional strict path does not.
field_trade_date_columns = {
    "price": "trade_date", "change_pct": "trade_date", "amount": "trade_date",
    "pe_ratio": "daily_basic_trade_date", "pb_ratio": "daily_basic_trade_date",
    "total_mv": "daily_basic_trade_date", "circ_mv": "daily_basic_trade_date",
    "turnover_rate": "daily_basic_trade_date", "volume_ratio": "daily_basic_trade_date",
}
```

`_annotate_snapshot_data_dates` must include the independent basics date (summary stays informational, not every-row verification). Add just the above evidence attrs to the existing cache metadata allowlist/readback. Absent legacy metadata stays absent. New metadata survives roundtrip, dates remain row-specific, and cache writes/readers do not invent `evidence_id` or verified universe/calendar flags. Existing cache age, source priority, fallback and industry semantics remain intact. Validate copy/no-mutation boundaries.

- [x] Step 4: Focused GREEN plus old snapshot/core tests, syntax and whitespace checks. Record commands and output in report, using only `.venv/Scripts/python.exe`.
- [x] Step 5: Self-review then independent task gate; no commit. Full report names exact files/risks and indicates that changed TTM values can legitimately change rankings.

## Task 2: 声明股票池的原始表诊断桥接

**Files:** Create `src/services/research_delivery/tushare_snapshot.py`, `tests/test_research_delivery_tushare.py`; Modify `src/services/screening/snapshot.py` (strict joins and invalid numeric-type guards only), `tests/test_tushare_snapshot_evidence.py` (strict join/type regressions), `docs/feishu-research-delivery.md`, `docs/CHANGELOG.md` and current plan progress.

**Interfaces:** Consume Task 1 `_prepare_tushare_snapshot(..., preserve_invalid_rows=True)` and existing `validate_snapshot`, `CORE_FIELDS`, `freeze_scope`, `assess_readiness`.

```python
def assess_tushare_snapshot(daily, daily_basic, stock_basic, *,
                            trade_date, universe, calendar_verified, universe_verified):
    """Return a ReadinessResult for captured raw tables; never fetch or publish."""
```

This diagnostic uses the caller's independently declared canonical universe, never derives it from returned quotes. Reindex the normalized strict snapshot to the full universe including stocks lacking quotes. Missing quotes/indicators yield missing/invalid receipts, not silently reduced counts. Keep available basic/reference cells even if a quote is absent: extend both `_prepare_tushare_snapshot` joins with `how="outer" if preserve_invalid_rows else "left"`, retaining upfront identity checks and the single conversion path. Normal-mode row membership stays unchanged. Add direct strict-join tests; no duplicate normalization helper. Extra source codes may be dropped only to project the explicitly supplied scope; duplicate identity still rejects. Before filtering source columns preserve source data integrity; no mutation, ordinary PE fallback or source-date fabrication. Deterministic source evidence ID: SHA-256 of the selected raw endpoint tables and normalization version, computed without arbitrary object stringification; reject invalid unsupported raw types. It is an input fingerprint, not authenticity or durable original-response storage.

- [x] Step 1: TDD tests use raw endpoint tables from Task 1 contracts, not ready-made valid receipts. Assert:

```python
result = assess_tushare_snapshot(daily, basics, reference,
    trade_date=date(2026, 10, 13), universe=("600000", "000001"),
    calendar_verified=True, universe_verified=True)
assert result.required_count == 2 * len(CORE_FIELDS)
assert result.state == "blocked"  # undated stock_basic.name remains unresolved
assert any("name" in reason for reason in result.blockers)
# T-1 basics produce wrong-date blockers for valuation/turnover, not quote fields.
# No quote for one declared stock still yields the original required_count.
# Unknown calendar/universe remain blocked even with all numeric evidence present.
```

Also cover malformed universe, source duplicates, invalid price/units, bool numeric input, nonfinite input, empty optional reference, missing/negative TTM, no mutation and all external facilities monkeypatched to raise if called. Dates/datetime64/timedelta values in numeric source columns must not be converted to apparently valid epoch/duration integers; mask these invalid numeric types before `pd.to_numeric`, preserving native date objects in business-date columns. Helpers must be minimal and focused; errors are fixed codes, not raw frames/credentials in messages. No CLI/network/API entrypoint is added in this task.

- [x] Step 2: Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_research_delivery_tushare.py --tb=short`; record RED.
- [x] Step 3: Implement projection -> actual collector transform -> field receipts -> existing readiness logic; never run filters to reduce coverage first. Source identity is `tushare`; evidence fingerprint is scoped to actual raw records. Schema failures raise `ValueError` with fixed messages, missing numeric observations are assessed blocked. No metadata added to old cache and no implicit calendar verification. No artificial ready test by assigning name dates.
- [x] Step 4: Run new tests + all research_delivery tests, `test_screening_compute_only.py`, `test_builtin_screening_engine.py`, `test_core_reliability.py`, `test_screening_daily_freshness.py`, `test_screening_explanations.py`, `test_screening_selection_variant.py`. Main controller will perform one final combined regression. Do not repeat full suite during local iterations.
- [x] Step 5: Document actual behavior, official field definitions/URLs, remaining name/date and independent status gaps, validation and surgical rollback. Add flat changelog entries. Task gate then final scoped review; no server/credentials/scheduler changes.

## Self-review and scope coverage

This is W1's snapshot evidence subset. W1 history/status/calendar persistence and W2—W6 remain covered by the existing roadmap, not by this local plan. Task 1 produces the same field/date/unit schema Task 2 consumes; it leaves normal price filtering as the default. Diagnostic always exposes unresolved name dates, not a fake fully ready snapshot. No live validation or profitable-strategy claim is part of acceptance.
