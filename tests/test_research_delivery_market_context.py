"""Deterministic shared-context verification; never requests providers."""
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.services.research_delivery.evidence_store import EvidenceStore
from src.services.research_delivery.market_context import build_market_context, load_market_context


DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


def tables():
    reference = pd.DataFrame([
        dict(ts_code="601166.SH", name="兴业银行", industry="银行"),
        dict(ts_code="600036.SH", name="招商银行", industry="银行"),
        dict(ts_code="600001.SH", name=None, industry="机械"),
    ])
    daily = pd.DataFrame([dict(ts_code=c, trade_date="20261015", pct_chg=p, amount=10.)
                          for c, p in [("601166.SH", .6), ("600036.SH", 1.4), ("600001.SH", -1.)]])
    return reference, daily


def batches(tmp_path):
    reference, daily = tables()
    store = EvidenceStore(tmp_path)
    evidence = [store.capture(endpoint, params, table, fetched_at=NOW, business_date=DAY)
        for endpoint, params, table in [
            ("stock_basic", dict(exchange="", list_status="L"), reference),
            ("daily", dict(trade_date="20261015"), daily)]]
    return [dict(business_date=DAY.isoformat(), state="degraded", blockers=[], evidence=evidence,
        coverage=dict(independent_universe_count=3, daily_response_count=3, daily_response_coverage=1.))]


def test_current_industry_context_has_human_names():
    reference, daily = tables()
    result = build_market_context(reference, daily, business_date=DAY.isoformat(),
        fetched_at=NOW.isoformat(), source="tushare")
    assert result["version"] == "research-context-v1"
    assert result["instruments"]["601166"]["name"] == "兴业银行"
    assert result["instruments"]["600001"]["name"] is None
    assert result["instruments"]["600001"]["name_status"] == "unknown"
    assert result["industries"]["source"] == "computed_from_tushare"
    assert result["industries"]["as_of"] == DAY.isoformat()
    assert result["industries"]["top"][0]["name"] == "银行"
    assert result["industries"]["top"][0]["change_pct"] == 1.
    assert result["industries"]["membership_is_point_in_time"] is False


def test_v2_evidence_recovery_never_restores_legacy_industry_grouping(tmp_path):
    sources = batches(tmp_path)
    sources[0]['coverage']['contract_version'] = 2
    result = load_market_context(sources, business_date=DAY, evidence_dir=tmp_path, now=NOW)
    assert result['industries'] == {} and result['industry_status'] == 'unavailable'
    assert result['market_breadth']['observed_count'] == 3
    assert 'canonical_industry_unassessed' in result['missing']
    legacy = deepcopy(sources[0])
    legacy['coverage'].pop('contract_version')
    mixed = load_market_context([legacy, *sources], business_date=DAY, evidence_dir=tmp_path, now=NOW)
    assert mixed['industries'] == {} and 'canonical_industry_unassessed' in mixed['missing']


def test_old_daily_or_reference_stamp_is_rejected():
    reference, daily = tables()
    daily.loc[0, "trade_date"] = "20261012"
    with pytest.raises(ValueError):
        build_market_context(reference, daily, business_date=DAY, fetched_at=NOW, source="tushare")
    with pytest.raises(ValueError):
        build_market_context(reference, daily.iloc[1:], business_date=DAY,
            fetched_at="2026-10-12T18:00:00+08:00", source="tushare")


def test_reference_observation_is_not_a_historical_membership_claim():
    reference, daily = tables()
    reference.loc[0, "name"] = "UNKNOWN"
    original_reference, original_daily = reference.copy(deep=True), daily.copy(deep=True)
    result = build_market_context(reference, daily, business_date=DAY, fetched_at=NOW, source="tushare")
    assert result["instruments"]["601166"]["status"] == "unknown"
    assert result["industries"]["population_basis"] == "observed_daily_traded_stocks"
    pd.testing.assert_frame_equal(reference, original_reference)
    pd.testing.assert_frame_equal(daily, original_daily)


def test_incomplete_metrics_do_not_block_existing_strategy_eligibility():
    reference, daily = tables()
    daily.loc[0, "amount"] = None
    result = build_market_context(reference, daily, business_date=DAY, fetched_at=NOW, source="tushare")
    assert result["industries"]
    assert result["market_breadth"] == {}
    assert "market_breadth_metrics_invalid" in result["missing"]


@pytest.mark.parametrize("field", ["amount", "pct_chg"])
@pytest.mark.parametrize("missing", [None, pd.NA])
def test_object_dtype_missing_metrics_preserve_context(field, missing):
    reference, daily = tables()
    daily[field] = daily[field].astype(object)
    daily.loc[0, field] = missing
    assert daily[field].dtype == object
    result = build_market_context(reference, daily, business_date=DAY, fetched_at=NOW, source="tushare")
    assert result["industries"] and result["instruments"]["601166"]["name"] == "兴业银行"
    assert result["market_breadth"] == {}
    assert "market_breadth_metrics_invalid" in result["missing"]


def test_industry_constituent_direction_counts_preserve_normalizer_ranking():
    from data_provider.tushare_fact_normalizers import industry_facts
    reference, daily = tables()
    daily.loc[1, "pct_chg"] = -.2
    reference = pd.concat([reference, pd.DataFrame([
        dict(ts_code="600004.SH", name="平盘", industry="银行"),
        dict(ts_code="600005.SH", name="缺收益", industry="银行"),
        dict(ts_code="600006.SH", name="无行业", industry=None),
    ])], ignore_index=True)
    daily = pd.concat([daily, pd.DataFrame([
        dict(ts_code="600004.SH", trade_date="20261015", pct_chg=0., amount=10.),
        dict(ts_code="600005.SH", trade_date="20261015", pct_chg=None, amount=10.),
        dict(ts_code="600006.SH", trade_date="20261015", pct_chg=2., amount=10.),
    ])], ignore_index=True)
    expected, _ = industry_facts(reference, daily, pd.DataFrame(), "20261015")
    result = build_market_context(reference, daily, business_date=DAY, fetched_at=NOW, source="tushare")
    for group in ("all", "top", "bottom"):
        assert [(r["name"], r["rank"], r["change_pct"]) for r in result["industries"][group]] == [
            (r["name"], r["rank"], r["change_pct"]) for r in expected[group]]
        bank = next(row for row in result["industries"][group] if row["name"] == "银行")
        assert (bank["advancers"], bank["decliners"], bank["flat"], bank["unknown_count"]) == (1, 1, 1, 1)
        assert bank["as_of"] == DAY.isoformat()
        assert bank["source"] == "computed_from_tushare"
    assert result["industries"]["mapped_count"] == 5


@pytest.mark.parametrize("payload", [None, [], "not-a-table"])
def test_malformed_evidence_does_not_escape_or_trust_attached_context(tmp_path, payload):
    stock_batches = batches(tmp_path)
    entry = stock_batches[0]["evidence"][0]
    raw = json.dumps(payload).encode()
    Path(entry["path"]).write_bytes(raw)
    entry["sha256"] = sha256(raw).hexdigest()
    result = load_market_context(stock_batches, business_date=DAY, evidence_dir=tmp_path, now=NOW)
    assert not result["instruments"] and not result["industries"]
    assert result["rejections"]


def test_verified_context_and_reference_names_without_industry(tmp_path):
    stock_batches = batches(tmp_path)
    result = load_market_context(stock_batches, business_date=DAY, evidence_dir=tmp_path, now=NOW)
    assert result["industry_status"] == "available"
    bank = next(row for row in result["industries"]["all"] if row["name"] == "银行")
    assert (bank["advancers"], bank["decliners"]) == (2, 0)
    assert result["instruments"]["601166"]["evidence_id"] == stock_batches[0]["evidence"][0]["sha256"]
    assert len(result["evidence"]) == 2
    path = Path(stock_batches[0]["evidence"][0]["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["columns"].remove("industry")
    payload["rows"] = [row[:2] for row in payload["rows"]]
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    stock_batches[0]["evidence"][0]["sha256"] = sha256(raw).hexdigest()
    result = load_market_context(stock_batches, business_date=DAY, evidence_dir=tmp_path, now=NOW)
    assert result["instruments"]["601166"]["name"] == "兴业银行"
    assert result["industries"] == {}
    assert "industry_membership_unavailable" in result["missing"]


@pytest.mark.parametrize("damage", ["hash", "path", "old", "future", "reference_params", "coverage"])
def test_rejects_unverified_context_and_arbitrary_facts(tmp_path, damage):
    stock_batches = batches(tmp_path)
    if damage == "hash":
        stock_batches[0]["evidence"][0]["sha256"] = "0" * 64
    elif damage == "path":
        stock_batches[0]["evidence"][0]["path"] = str(tmp_path.parent / "outside.json")
    elif damage == "old":
        stock_batches[0]["business_date"] = "2026-10-12"
    elif damage == "coverage":
        stock_batches[0]["coverage"]["daily_response_coverage"] = .94
    else:
        entry = stock_batches[0]["evidence"][0]
        path = Path(entry["path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        if damage == "future":
            data["fetched_at"] = "2026-10-15T19:00:00+08:00"
        else:
            data["params"]["list_status"] = "D"
        raw = json.dumps(data).encode()
        path.write_bytes(raw)
        entry["sha256"] = sha256(raw).hexdigest()
    stock_batches[0]["result"] = dict(research_context=dict(industries=dict(all=[dict(name="伪造")])) )
    result = load_market_context(deepcopy(stock_batches), business_date=DAY, evidence_dir=tmp_path, now=NOW)
    assert not result["industries"]
    assert result["rejections"]
