"""Offline contract tests for production Tushare snapshot evidence."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import numpy as np
import pytest

from src.services.screening import snapshot


@pytest.fixture
def raw_tables():
    daily = pd.DataFrame({
        "ts_code": ["600000.SH", "000001.SZ"],
        "trade_date": ["20261013", "20261012"],
        "close": [10, 20], "pct_chg": [1, 2], "amount": [2000, 3000],
    })
    basic = pd.DataFrame({
        "ts_code": ["600000.SH", "000001.SZ"],
        "trade_date": ["20261012", "20261013"],
        "pe": [7, 8], "pe_ttm": [12, 13], "pb": [1, 2],
        "total_mv": [30000, 40000], "circ_mv": [20000, 25000],
        "turnover_rate": [1.5, 2.5], "volume_ratio": [1.1, 1.2],
    })
    stock = pd.DataFrame({
        "ts_code": ["600000.SH", "000001.SZ"], "symbol": ["600000", "000001"],
        "name": ["Bank A", "Bank B"], "industry": ["Bank", "Bank"],
    })
    return daily, basic, stock


def test_ttm_units_and_independent_row_dates(raw_tables):
    out = snapshot._prepare_tushare_snapshot(*raw_tables)
    assert out["code"].tolist() == ["600000", "000001"]
    assert out.loc[0, "pe_ratio"] == 12
    assert out.loc[0, "amount"] == 2_000_000
    assert out.loc[0, "total_mv"] == 300_000_000
    assert out.loc[0, "circ_mv"] == 200_000_000
    assert out["trade_date"].tolist() == ["20261013", "20261012"]
    assert out["daily_basic_trade_date"].tolist() == ["20261012", "20261013"]
    assert out.attrs["units_profile"] == "cn_equity_cny_v1"
    assert out.attrs["pe_ratio_basis"] == "ttm"
    assert out.attrs["field_trade_date_columns"] == {
        "price": "trade_date", "change_pct": "trade_date", "amount": "trade_date",
        "pe_ratio": "daily_basic_trade_date", "pb_ratio": "daily_basic_trade_date",
        "total_mv": "daily_basic_trade_date", "circ_mv": "daily_basic_trade_date",
        "turnover_rate": "daily_basic_trade_date", "volume_ratio": "daily_basic_trade_date",
        "dividend_yield_ttm_pct": "daily_basic_trade_date",
    }
    assert "name" not in out.attrs["field_trade_date_columns"]
    assert out.attrs['dividend_yield_ttm_unit'] == 'percent'
    assert out.dividend_yield_ttm_pct.isna().all()


@pytest.mark.parametrize("value", [None, -5])
def test_missing_and_negative_ttm_are_not_replaced(raw_tables, value):
    daily, basic, stock = raw_tables
    basic.loc[0, "pe_ttm"] = value
    out = snapshot._prepare_tushare_snapshot(daily, basic, stock)
    if value is None:
        assert pd.isna(out.loc[0, "pe_ratio"])
    else:
        assert out.loc[0, "pe_ratio"] == value


def test_absent_ttm_never_uses_static_pe(raw_tables):
    daily, basic, stock = raw_tables
    out = snapshot._prepare_tushare_snapshot(daily, basic.drop(columns="pe_ttm"), stock)
    assert out["pe_ratio"].isna().all()


@pytest.mark.parametrize("missing", ["row", "date", "null_date"])
def test_missing_basic_evidence_stays_missing(raw_tables, missing):
    daily, basic, stock = raw_tables
    if missing == "row":
        basic = basic.iloc[1:]
    elif missing == "date":
        basic = basic.drop(columns="trade_date")
    else:
        basic.loc[0, "trade_date"] = None
    out = snapshot._prepare_tushare_snapshot(daily, basic, stock)
    assert pd.isna(out.loc[0, "daily_basic_trade_date"])
    assert out.loc[0, "trade_date"] == "20261013"
    if missing == "row":
        assert pd.isna(out.loc[0, "pe_ratio"])


@pytest.mark.parametrize("table_index", [0, 1, 2])
@pytest.mark.parametrize("invalid", ["duplicate_code", "null_code", "duplicate_columns"])
def test_rejects_ambiguous_tables_before_join(raw_tables, table_index, invalid, monkeypatch):
    tables = list(raw_tables)
    table = tables[table_index].copy()
    if invalid == "duplicate_code":
        table.loc[1, "ts_code"] = table.loc[0, "ts_code"]
    elif invalid == "null_code":
        table.loc[0, "ts_code"] = None
    else:
        table = pd.concat([table, table[["ts_code"]]], axis=1)
    tables[table_index] = table
    merge = Mock(side_effect=AssertionError("invalid inputs must be rejected before merge"))
    monkeypatch.setattr(pd.DataFrame, "merge", merge)
    with pytest.raises(ValueError):
        snapshot._prepare_tushare_snapshot(*tables)
    merge.assert_not_called()


@pytest.mark.parametrize("table_index", [0, 1, 2])
def test_symbol_conflicts_rejected(raw_tables, table_index):
    raw_tables[table_index]["symbol"] = ["999999", "000001"]
    with pytest.raises(ValueError, match="symbol"):
        snapshot._prepare_tushare_snapshot(*raw_tables)


@pytest.mark.parametrize("reference", ["none", "empty", "no_symbol", "null_symbol"])
def test_code_identity_does_not_depend_on_reference_symbol(raw_tables, reference):
    daily, basic, stock = raw_tables
    if reference == "none":
        stock = None
    elif reference == "empty":
        stock = pd.DataFrame()
    elif reference == "no_symbol":
        stock = stock.drop(columns="symbol")
    else:
        stock["symbol"] = None
    out = snapshot._prepare_tushare_snapshot(daily, basic, stock)
    assert out["code"].tolist() == ["600000", "000001"]


def test_endpoint_owned_columns_and_copy_boundary(raw_tables):
    daily, basic, stock = raw_tables
    daily["pb"] = 999
    basic["close"] = 999
    stock["trade_date"] = "19000101"
    for table in raw_tables:
        table.attrs["nested"] = {"values": ["original"]}
    originals = deepcopy(raw_tables)
    out = snapshot._prepare_tushare_snapshot(*raw_tables)
    assert out.loc[0, "price"] == 10
    assert out.loc[0, "pb_ratio"] == 1
    assert out.loc[0, "trade_date"] == "20261013"
    out.attrs.setdefault("nested", {"values": []})["values"].append("output")
    for table, original in zip(raw_tables, originals):
        pd.testing.assert_frame_equal(table, original)
        assert table.attrs == original.attrs


@pytest.mark.parametrize("column,table_index,standard", [
    ("close", 0, "price"), ("pct_chg", 0, "change_pct"), ("amount", 0, "amount"),
    ("pe_ttm", 1, "pe_ratio"), ("pb", 1, "pb_ratio"),
    ("total_mv", 1, "total_mv"), ("circ_mv", 1, "circ_mv"),
    ("volume_ratio", 1, "volume_ratio"), ("turnover_rate", 1, "turnover_rate"),
])
def test_boolean_is_not_numeric_evidence(raw_tables, column, table_index, standard):
    raw_tables[table_index][column] = pd.Series([True, 2], dtype=object)
    out = snapshot._prepare_tushare_snapshot(*raw_tables, preserve_invalid_rows=True)
    assert pd.isna(out.loc[0, standard])


@pytest.mark.parametrize("price", [None, 0, -1, True])
def test_invalid_prices_retained_only_when_explicit(raw_tables, price):
    raw_tables[0]["close"] = pd.Series([price, 20], dtype=object)
    default = snapshot._prepare_tushare_snapshot(*raw_tables)
    strict = snapshot._prepare_tushare_snapshot(*raw_tables, preserve_invalid_rows=True)
    assert default["code"].tolist() == ["000001"]
    assert strict["code"].tolist() == ["600000", "000001"]


def test_strict_outer_joins_keep_basic_and_reference_without_quote(raw_tables):
    daily, basic, stock = raw_tables
    daily = daily.iloc[:1].copy()
    extra_reference = stock.iloc[:1].copy()
    extra_reference["ts_code"] = "600001.SH"
    extra_reference["symbol"] = "600001"
    extra_reference["name"] = "Reference only"
    stock = pd.concat([stock, extra_reference], ignore_index=True)
    default = snapshot._prepare_tushare_snapshot(daily, basic, stock)
    strict = snapshot._prepare_tushare_snapshot(daily, basic, stock, preserve_invalid_rows=True).set_index("code")
    assert default["code"].tolist() == ["600000"]
    assert set(strict.index) == {"600000", "000001", "600001"}
    assert strict.index.tolist() == ["600000", "000001", "600001"]
    assert pd.isna(strict.loc["000001", "price"])
    assert strict.loc["000001", "pe_ratio"] == 13
    assert strict.loc["000001", "daily_basic_trade_date"] == "20261013"
    assert strict.loc["600001", "name"] == "Reference only"
    assert pd.isna(strict.loc["600001", "price"])
    assert pd.isna(strict.loc["600001", "pe_ratio"])


@pytest.mark.parametrize("column,endpoint,standard", [
    ("close", 0, "price"), ("pct_chg", 0, "change_pct"), ("amount", 0, "amount"),
    ("pe_ttm", 1, "pe_ratio"), ("pb", 1, "pb_ratio"), ("total_mv", 1, "total_mv"),
    ("circ_mv", 1, "circ_mv"), ("volume_ratio", 1, "volume_ratio"), ("turnover_rate", 1, "turnover_rate"),
])
@pytest.mark.parametrize("value", [timedelta(days=1), pd.Timedelta(days=1), np.timedelta64(1, "D")])
def test_duration_cannot_be_nanosecond_numeric_evidence(raw_tables, column, endpoint, standard, value):
    raw_tables[endpoint][column] = [value, value]
    out = snapshot._prepare_tushare_snapshot(*raw_tables, preserve_invalid_rows=True).set_index("code")
    assert pd.isna(out.loc["600000", standard])


@pytest.mark.parametrize("source,price_column", [
    ("sina", "trade"), ("efinance", "最新价"), ("akshare_em", "最新价"),
    ("em_datacenter", "NEW_PRICE"), ("unknown", "price"),
])
def test_non_tushare_normalization_defaults_unchanged(source, price_column):
    frame = pd.DataFrame({price_column: [None, 0, -1, 10], "marker": range(4)})
    assert snapshot._normalize(frame, source)["marker"].tolist() == [3]
    assert len(snapshot._normalize(frame, source, preserve_invalid_rows=True)) == 4
    assert "pe_ratio_basis" not in snapshot._normalize(frame, source).attrs


def test_normalize_does_not_mutate_input_values_or_nested_attrs():
    frame = pd.DataFrame({"close": [True, 10], "pe_ttm": [False, -5]})
    frame.attrs["nested"] = {"values": ["original"]}
    original = deepcopy(frame)
    out = snapshot._normalize(frame, "tushare", preserve_invalid_rows=True)
    out.attrs["nested"]["values"].append("output")
    assert pd.isna(out.loc[0, "price"])
    assert pd.isna(out.loc[0, "pe_ratio"])
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original.attrs


def test_production_request_uses_ttm_and_basic_date(raw_tables, monkeypatch):
    daily, basic, stock = raw_tables
    pro = SimpleNamespace(daily=Mock(return_value=daily), daily_basic=Mock(return_value=basic),
                          stock_basic=Mock(return_value=stock))
    monkeypatch.setenv("TUSHARE_TOKEN", "offline-test-token")
    monkeypatch.setenv("TUSHARE_TRADE_DATE", "20261013")
    factory = Mock(return_value=pro)
    monkeypatch.setattr("data_provider.tushare_fetcher.create_tushare_http_client", factory)
    out = snapshot._fetch_tushare()
    factory.assert_called_once_with("offline-test-token")
    pro.daily_basic.assert_called_once_with(
        trade_date="20261013",
        fields="ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv",
    )
    assert out.loc[0, "pe_ratio"] == 12


def test_cache_roundtrip_preserves_only_supported_evidence(raw_tables, tmp_path):
    out = snapshot._prepare_tushare_snapshot(*raw_tables)
    snapshot._annotate_snapshot_data_dates(out)
    assert out.attrs["snapshot_data_dates"] == {
        "trade_date": "2026-10-13", "daily_basic_trade_date": "2026-10-13",
    }
    out.attrs.update(evidence_id="not-cache-evidence", universe_verified=True, calendar_verified=True)
    original = deepcopy(out)
    path = tmp_path / "snapshot.json"
    snapshot._write_last_good_snapshot(path, out, source_priority=["tushare", "sina"])
    cached = snapshot._read_last_good_snapshot(
        path, required_columns=["price"], source_errors=[], fresh=True,
        max_age_hours=1, requested_snapshot_sources=["tushare", "sina"],
    )
    assert cached is not None
    assert cached["trade_date"].tolist() == out["trade_date"].tolist()
    assert cached["daily_basic_trade_date"].tolist() == out["daily_basic_trade_date"].tolist()
    for key in ("units_profile", "pe_ratio_basis", "field_trade_date_columns", "snapshot_data_dates"):
        assert cached.attrs[key] == out.attrs[key]
    for key in ("evidence_id", "universe_verified", "calendar_verified"):
        assert key not in cached.attrs
    pd.testing.assert_frame_equal(out, original)
    assert out.attrs == original.attrs


def test_legacy_cache_does_not_invent_evidence_attrs(tmp_path):
    frame = pd.DataFrame({"code": ["600000"], "price": [10]})
    frame.attrs["snapshot_source"] = "tushare"
    path = tmp_path / "legacy.json"
    snapshot._write_last_good_snapshot(path, frame)
    cached = snapshot._read_last_good_snapshot(path, required_columns=[], source_errors=[])
    assert cached is not None
    for key in ("units_profile", "pe_ratio_basis", "field_trade_date_columns", "evidence_id",
                "universe_verified", "calendar_verified"):
        assert key not in cached.attrs
