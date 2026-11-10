"""Captured endpoint fixtures are synthetic; no online or publication acceptance."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from src.services.research_delivery import tushare_snapshot as adapter
from src.services.research_delivery.evidence import CORE_FIELDS


DAY = date(2026, 10, 13)
UNIVERSE = ("600000", "000001")


@pytest.fixture
def tables():
    return (
        pd.DataFrame(dict(ts_code=["600000.SH", "000001.SZ"], trade_date=["20261013"] * 2,
                          close=[10., 20.], pct_chg=[1., 2.], amount=[2000., 3000.])),
        pd.DataFrame(dict(ts_code=["600000.SH", "000001.SZ"], trade_date=["20261013"] * 2,
                          pe=[7., 8.], pe_ttm=[12., 13.], pb=[1., 2.], total_mv=[30000., 40000.],
                          circ_mv=[20000., 25000.], turnover_rate=[1.5, 2.5], volume_ratio=[1.1, 1.2])),
        pd.DataFrame(dict(ts_code=["600000.SH", "000001.SZ"], symbol=list(UNIVERSE),
                          name=["Bank A", "Bank B"], industry=["Bank", "Bank"])),
    )


def assess(tables, **overrides):
    options = dict(trade_date=DAY, universe=UNIVERSE, calendar_verified=True, universe_verified=True)
    options.update(overrides)
    return adapter.assess_tushare_snapshot(*tables, **options)


@pytest.fixture
def captured(monkeypatch):
    observed = []
    real = adapter.validate_snapshot
    def validate(*args):
        result = real(*args)
        observed.append((args[0].copy(deep=True), result[1]))
        return result
    monkeypatch.setattr(adapter, "validate_snapshot", validate)
    return observed


def test_raw_tables_use_real_units_ttm_and_never_date_name(tables, captured):
    result = assess(tables)
    assert result.required_count == 2 * len(CORE_FIELDS)
    assert result.valid_count == 2 * (len(CORE_FIELDS) - 1)
    assert result.state == "blocked"
    assert set(result.blockers) == {f"snapshot:{code}:name:wrong_or_unknown_business_date" for code in UNIVERSE}
    frame, receipts = captured[0]
    assert frame.loc[0, "amount"] == 2_000_000
    assert frame.loc[0, "total_mv"] == 300_000_000
    assert frame.loc[0, "pe_ratio"] == 12
    assert all(item.source == "tushare" and len(item.evidence_id) == 64 for item in receipts)
    assert all(item.as_of is None for item in receipts if item.key.field == "name")


def test_t_minus_one_basic_dates_do_not_contaminate_quotes(tables):
    tables[1].loc[0, "trade_date"] = "20261012"
    result = assess(tables)
    for field in ("pe_ratio", "pb_ratio", "total_mv", "turnover_rate", "volume_ratio"):
        assert f"snapshot:600000:{field}:wrong_or_unknown_business_date" in result.blockers
    assert not any(f"600000:{field}:" in reason for reason in result.blockers for field in ("price", "amount", "change_pct"))


@pytest.mark.parametrize("missing_endpoint", [0, 1, 2, "all"])
def test_full_universe_preserves_available_cells_without_quotes(tables, missing_endpoint, captured):
    sparse = tuple(table.iloc[:1].copy() if missing_endpoint in (index, "all") else table
                   for index, table in enumerate(tables))
    result = assess(sparse)
    assert result.required_count == 2 * len(CORE_FIELDS)
    frame, receipts = captured[0]
    assert frame.code.tolist() == list(UNIVERSE)
    if missing_endpoint == 0:
        assert frame.loc[1, "pe_ratio"] == 13
        assert frame.loc[1, "name"] == "Bank B"
        assert result.valid_count == 13
        assert "snapshot:000001:price:invalid" in result.blockers
    if missing_endpoint == 1:
        assert frame.loc[1, "price"] == 20
        assert "snapshot:000001:pe_ratio:invalid" in result.blockers
    assert len(receipts) == result.required_count


@pytest.mark.parametrize("reference", [None, pd.DataFrame()])
def test_empty_optional_reference_is_missing_name_not_schema_failure(tables, reference):
    result = assess((*tables[:2], reference))
    assert result.valid_count == 16
    assert "snapshot:600000:name:invalid" in result.blockers


@pytest.mark.parametrize("endpoint", [0, 1, 2])
def test_empty_endpoint_with_identity_column_retains_scope(tables, endpoint):
    frames = list(tables)
    frames[endpoint] = pd.DataFrame({"ts_code": []})
    result = assess(frames)
    assert result.state == "blocked" and result.required_count == 18


def test_unknown_dates_are_blockers_not_fabricated(tables):
    tables[0].loc[0, "trade_date"] = None
    tables[1].loc[0, "trade_date"] = "2026/10/13"
    result = assess(tables)
    assert "snapshot:600000:price:wrong_or_unknown_business_date" in result.blockers
    assert "snapshot:600000:pe_ratio:wrong_or_unknown_business_date" in result.blockers


@pytest.mark.parametrize("flag,blocker", [("calendar_verified", "calendar_unknown"),
                                        ("universe_verified", "universe_or_requirements_unknown")])
def test_independent_verification_is_not_inferred(tables, flag, blocker):
    result = assess(tables, **{flag: False})
    assert result.state == "blocked" and blocker in result.blockers
    assert result.valid_count == 16 and result.required_count == 18


@pytest.mark.parametrize("universe", [(), "600000", {"600000"}, ("600000", "600000"),
                                    ("600000", "1"), (600000,), None])
def test_malformed_universe_fixed_error(tables, universe):
    with pytest.raises(ValueError, match="^tushare_universe_invalid$"):
        assess(tables, universe=universe)


@pytest.mark.parametrize("override", [dict(trade_date="20261013"), dict(trade_date=datetime(2026, 10, 13)),
                                     dict(calendar_verified=1), dict(universe_verified="yes")])
def test_bad_request_types_fixed_error(tables, override):
    with pytest.raises(ValueError, match="^tushare_request_invalid$"):
        assess(tables, **override)


@pytest.mark.parametrize("endpoint", [0, 1, 2])
@pytest.mark.parametrize("damage", ["duplicate_rows", "duplicate_columns", "missing_identity", "symbol_conflict", "bad_code"])
def test_source_integrity_checked_before_projection(tables, endpoint, damage):
    frames = list(tables)
    frame = frames[endpoint]
    if damage == "duplicate_rows":
        extra = frame.iloc[:1].copy()
        extra["ts_code"] = "999999.SH"
        if "symbol" in extra:
            extra["symbol"] = "999999"
        frames[endpoint] = pd.concat([frame, extra, extra], ignore_index=True)
    elif damage == "duplicate_columns":
        frames[endpoint] = pd.concat([frame, frame[["ts_code"]]], axis=1)
    elif damage == "missing_identity":
        frames[endpoint] = frame.drop(columns="ts_code")
    elif damage == "symbol_conflict":
        frame["symbol"] = ["000001", "000001"]
    else:
        frame.loc[0, "ts_code"] = "secret-token"
    with pytest.raises(ValueError, match="^tushare_source_schema_invalid$"):
        assess(frames)


def test_cross_endpoint_canonical_identity_conflict(tables):
    tables[1].loc[0, "ts_code"] = "600000.SZ"
    with pytest.raises(ValueError, match="^tushare_source_schema_invalid$"):
        assess(tables)


@pytest.mark.parametrize("endpoint", [0, 1, 2])
def test_table_type_failure_fixed_error(tables, endpoint):
    frames = list(tables)
    frames[endpoint] = {"secret": "credentials"}
    with pytest.raises(ValueError, match="^tushare_source_schema_invalid$"):
        assess(frames)


@pytest.mark.parametrize("endpoint,column,field", [(0, "close", "price"), (0, "amount", "amount"),
                                                  (1, "total_mv", "total_mv"), (1, "pe_ttm", "pe_ratio"),
                                                  (1, "turnover_rate", "turnover_rate")])
@pytest.mark.parametrize("value", [None, np.nan, np.inf, -np.inf, True, "not-a-number"])
def test_numeric_missing_invalid_bool_and_nonfinite_block(tables, endpoint, column, field, value):
    tables[endpoint][column] = pd.Series([value, 20], dtype=object)
    result = assess(tables)
    assert result.required_count == 18
    assert f"snapshot:600000:{field}:invalid" in result.blockers


@pytest.mark.parametrize("endpoint,column,field", [
    (0, "close", "price"), (0, "pct_chg", "change_pct"), (0, "amount", "amount"),
    (1, "total_mv", "total_mv"), (1, "circ_mv", "circ_mv"), (1, "pe_ttm", "pe_ratio"),
    (1, "pb", "pb_ratio"), (1, "turnover_rate", "turnover_rate"), (1, "volume_ratio", "volume_ratio"),
])
@pytest.mark.parametrize("value", [DAY, datetime(2026, 10, 13), pd.Timestamp("2026-10-13")])
def test_date_typed_numeric_source_is_not_nanosecond_evidence(tables, endpoint, column, field, value):
    tables[endpoint][column] = [value, value]
    result = assess(tables)
    if field in CORE_FIELDS:
        assert f"snapshot:600000:{field}:invalid" in result.blockers
    else:
        frame = adapter._prepare_tushare_snapshot(*tables, preserve_invalid_rows=True).set_index("code")
        assert pd.isna(frame.loc["600000", field])


def test_native_source_dates_remain_legitimate_business_dates(tables):
    tables[0]["trade_date"] = [DAY, DAY]
    tables[1]["trade_date"] = pd.to_datetime(["2026-10-13", "2026-10-13"])
    assert assess(tables).valid_count == 16


@pytest.mark.parametrize("duration", [timedelta(days=1), pd.Timedelta(days=1), np.timedelta64(1, "D")])
def test_duration_raw_scalar_rejected_with_fixed_code(tables, duration):
    tables[0]["close"] = [duration, duration]
    with pytest.raises(ValueError, match="^tushare_source_scalar_invalid$"):
        assess(tables)


@pytest.mark.parametrize("unit", ["D", "ns"])
@pytest.mark.parametrize("endpoint,column", [(0, "close"), (1, "pe_ttm"), (2, "name")])
@pytest.mark.parametrize("location", ["owned", "ignored", "out_of_scope"])
def test_object_numpy_duration_rejected_before_projection(tables, unit, endpoint, column, location):
    frames = list(tables)
    frame = frames[endpoint].copy()
    if location == "ignored":
        column = "unused"
    if location == "out_of_scope":
        extra = frame.iloc[:1].copy()
        extra["ts_code"] = "999999.SH"
        if "symbol" in extra:
            extra["symbol"] = "999999"
        frame = pd.concat([frame, extra], ignore_index=True)
    values = frame[column].tolist() if column in frame else [None] * len(frame)
    values[-1 if location == "out_of_scope" else 0] = np.timedelta64(1, unit)
    frame[column] = pd.Series(values, dtype=object)
    assert isinstance(frame[column].iloc[-1 if location == "out_of_scope" else 0], np.timedelta64)
    frames[endpoint] = frame
    with pytest.raises(ValueError, match="^tushare_source_scalar_invalid$"):
        assess(frames)


@pytest.mark.parametrize("unit", ["D", "ns"])
def test_fingerprint_rejects_object_numpy_duration_through_shared_encoder(tables, unit):
    tables[0]["close"] = pd.Series([np.timedelta64(1, unit), 20.], dtype=object)
    with pytest.raises(ValueError, match="^tushare_source_scalar_invalid$"):
        adapter._evidence_id(tables)


@pytest.mark.parametrize("endpoint,column,field,value", [(0, "close", "price", 0), (0, "close", "price", -1),
                                                       (0, "amount", "amount", -1), (1, "total_mv", "total_mv", -1)])
def test_price_and_unit_domains_block_without_filtering(tables, endpoint, column, field, value):
    tables[endpoint].loc[0, column] = value
    assert f"snapshot:600000:{field}:invalid" in assess(tables).blockers


def test_missing_ttm_never_uses_static_pe_and_negative_ttm_remains_observation(tables, captured):
    frames = (*tables[:1], tables[1].drop(columns="pe_ttm"), tables[2])
    assert "snapshot:600000:pe_ratio:invalid" in assess(frames).blockers
    tables[1].loc[0, "pe_ttm"] = -5
    result = assess(tables)
    assert not any("pe_ratio" in reason for reason in result.blockers)
    assert captured[-1][0].loc[0, "pe_ratio"] == -5


def test_unit_metadata_guard_uses_existing_validator(tables, monkeypatch):
    real = adapter._prepare_tushare_snapshot
    def corrupt(*args, **kwargs):
        frame = real(*args, **kwargs)
        frame.attrs["units_profile"] = "incorrect"
        return frame
    monkeypatch.setattr(adapter, "_prepare_tushare_snapshot", corrupt)
    assert "snapshot_units_profile_invalid" in assess(tables).blockers


def test_no_input_mutation(tables):
    for table in tables:
        table.attrs["nested"] = {"values": ["original"]}
    originals = deepcopy(tables)
    assess(tables)
    for table, original in zip(tables, originals):
        pd.testing.assert_frame_equal(table, original)
        assert table.attrs == original.attrs


def test_fingerprint_deterministic_scoped_raw_identity(tables, captured):
    assess(tables)
    baseline = captured[-1][0].attrs["evidence_id"]
    assess(tuple(frame.iloc[::-1].reset_index(drop=True) for frame in tables), universe=UNIVERSE[::-1])
    assert captured[-1][0].attrs["evidence_id"] == baseline
    assert captured[-1][0].code.tolist() == list(UNIVERSE[::-1])
    extra = []
    for frame in tables:
        row = frame.iloc[:1].copy()
        row["ts_code"] = "999999.SH"
        if "symbol" in row:
            row["symbol"] = "999999"
        extra.append(pd.concat([frame, row], ignore_index=True))
    assess(extra)
    assert captured[-1][0].attrs["evidence_id"] == baseline
    tables[0].loc[0, "close"] = 10.5
    assess(tables)
    assert captured[-1][0].attrs["evidence_id"] != baseline


def test_fingerprint_includes_normalization_version(tables, captured, monkeypatch):
    assess(tables)
    baseline = captured[-1][0].attrs["evidence_id"]
    monkeypatch.setattr(adapter, "_NORMALIZATION_VERSION", "different-version")
    assess(tables)
    assert captured[-1][0].attrs["evidence_id"] != baseline


@pytest.mark.parametrize("value", [None, np.nan, pd.NA, pd.NaT])
def test_missing_scalar_fingerprints_stably(tables, captured, value):
    tables[1]["pe_ttm"] = pd.Series([value, 13], dtype=object)
    assess(tables)
    first = captured[-1][0].attrs["evidence_id"]
    assess(deepcopy(tables))
    assert captured[-1][0].attrs["evidence_id"] == first


def test_arbitrary_raw_object_rejected_without_stringification(tables):
    class Unsafe:
        def __str__(self):
            pytest.fail("raw object stringified")
        def __repr__(self):
            pytest.fail("raw object repr used")
    tables[0]["unused"] = pd.Series([Unsafe(), "ok"], dtype=object)
    with pytest.raises(ValueError, match="^tushare_source_scalar_invalid$"):
        assess(tables)


def test_no_external_facilities_called(tables, monkeypatch):
    import os
    import requests
    import socket
    from data_provider import tushare_fetcher
    from src.services.screening import snapshot
    from src.notification import NotificationService
    forbidden = Mock(side_effect=AssertionError("external facility called"))
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(tushare_fetcher, "create_tushare_http_client", forbidden)
    monkeypatch.setattr(snapshot, "_fetch_tushare", forbidden)
    monkeypatch.setattr("src.services.screening.pipeline.screen", forbidden)
    monkeypatch.setattr("src.services.screening.ranker._call_llm", forbidden)
    monkeypatch.setattr(NotificationService, "send", forbidden)
    monkeypatch.setattr(NotificationService, "send_to_feishu", forbidden)
    assert assess(tables).state == "blocked"
    forbidden.assert_not_called()
