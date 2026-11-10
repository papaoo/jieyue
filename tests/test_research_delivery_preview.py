"""Strict preview tests use fixed-date synthetic evidence only."""
import copy
from datetime import date

import pandas as pd
import pytest

from src.services.research_delivery.screening_preview import preview_strategy
from src.services.screening.config import Config
from src.services.screening.strategy import list_strategies, load_all_strategies
from tests.test_screening_compute_only import snapshot, forbid_remote


T = date(2026, 10, 9)
CORE = ("name", "price", "amount", "total_mv", "pe_ratio", "pb_ratio", "turnover_rate", "volume_ratio", "change_pct")


def verified_snapshot(codes=("600001", "600002")):
    frame = snapshot(codes)
    frame["business_date"] = "20261009"
    frame.attrs.update(snapshot_source="synthetic", evidence_id="synthetic-batch",
                       units_profile="cn_equity_cny_v1", pe_ratio_basis="ttm",
                       field_trade_date_columns={field: "business_date" for field in CORE})
    return frame


def history(code="600001"):
    close = pd.Series([9. + i / 60 for i in range(61)])
    frame = pd.DataFrame(dict(date=pd.bdate_range(end=T, periods=61), open=close,
                              high=close + .1, low=close - .1, close=close, volume=1e6))
    frame.attrs.update(code=code, daily_source="synthetic", evidence_id="synthetic-history",
                       price_adjustment="forward", volume_unit="shares")
    return frame


def preview(frame=None, strategy="dual_low", **kwargs):
    frame = verified_snapshot() if frame is None else frame
    return preview_strategy(strategy, frame, trade_date=T, universe=kwargs.pop("universe", ("600001", "600002")),
                            calendar_verified=kwargs.pop("calendar_verified", True),
                            universe_verified=kwargs.pop("universe_verified", True), **kwargs)


def test_real_preview_numeric_strings_unpolluted_no_mutation(monkeypatch):
    forbid_remote(monkeypatch)
    frame = verified_snapshot()
    frame[list(CORE[1:])] = frame[list(CORE[1:])].astype(str)
    frame["signal_score"] = 100
    frame["board_heat_score"] = 100
    original = copy.deepcopy(frame)
    result = preview(frame, daily_histories={code: history(code) for code in frame.code})
    assert result.readiness.state == "degraded"
    assert result.result and len(result.result.picks) == 2
    assert not result.publish_allowed
    assert all(p.signal_score is not None and p.board_heat_score is None and p.industry_code == 'J66' for p in result.result.picks)
    assert result.readiness.valid_count == result.readiness.required_count
    assert result.readiness.warnings and result.result.degradation
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original.attrs


@pytest.mark.parametrize("damage", ["nan", "inf", "bool", "name", "date_missing", "date_stale", "date_future",
                                    "date_invalid", "source", "evidence", "units", "pe_basis", "duplicate",
                                    "missing_row", "extra_row", "empty_universe", "duplicate_universe", "invalid_universe"])
def test_snapshot_evidence_blocks_before_calculation(monkeypatch, damage):
    from src.services.research_delivery import screening_preview as adapter
    monkeypatch.setattr(adapter, "screen", lambda *a, **k: pytest.fail("blocked input calculated"))
    frame = verified_snapshot()
    kwargs = {}
    if damage in {"nan", "inf", "bool"}:
        frame["price"] = {"nan": float("nan"), "inf": float("inf"), "bool": True}[damage]
    elif damage == "name":
        frame["name"] = " "
    elif damage.startswith("date_"):
        if damage == "date_missing":
            del frame.attrs["field_trade_date_columns"]["price"]
        else:
            frame["business_date"] = {"date_stale": "2026-10-08", "date_future": "2026-10-12", "date_invalid": "2026/10/09"}[damage]
    elif damage in {"source", "evidence", "units", "pe_basis"}:
        frame.attrs[{"source": "snapshot_source", "evidence": "evidence_id", "units": "units_profile", "pe_basis": "pe_ratio_basis"}[damage]] = ""
    elif damage == "duplicate":
        frame["code"] = "600001"
    elif damage == "missing_row":
        frame = frame.iloc[:1].copy()
    elif damage == "extra_row":
        frame = verified_snapshot(("600001", "600002", "600003"))
    else:
        kwargs["universe"] = {"empty_universe": (), "duplicate_universe": ("600001", "600001"), "invalid_universe": ("1", "600002")}[damage]
    result = preview(frame, **kwargs)
    assert result.readiness.state == "blocked" and result.readiness.blockers
    assert result.result is None and not result.publish_allowed


@pytest.mark.parametrize("flag", ["calendar_verified", "universe_verified"])
def test_unverified_scope_never_calculates(monkeypatch, flag):
    from src.services.research_delivery import screening_preview as adapter
    monkeypatch.setattr(adapter, "screen", lambda *a, **k: pytest.fail("unknown scope calculated"))
    assert preview(**{flag: False}).readiness.state == "blocked"


def test_valid_empty_filter_is_success_but_invalid_empty_filter_is_blocked():
    frame = verified_snapshot()
    frame["amount"] = 0
    result = preview(frame)
    assert result.result is not None and not result.result.picks
    assert result.readiness.state == "degraded"
    frame["pe_ratio"] = float("nan")
    assert preview(frame).result is None


def test_real_daily_preview_preserves_planned_scope_and_raw_input(monkeypatch):
    forbid_remote(monkeypatch)
    histories = {code: history(code) for code in ("600001", "600002")}
    original = copy.deepcopy(histories)
    result = preview(strategy="shrink_pullback", daily_histories=histories)
    assert result.result is not None and result.result.daily_enriched
    assert result.scope.technical_planned == ("600001", "600002")
    assert result.readiness.state == "degraded"
    for code in histories:
        pd.testing.assert_frame_equal(histories[code], original[code])
        assert histories[code].attrs == original[code].attrs


@pytest.mark.parametrize("damage", ["missing", "ohlc", "volume", "short", "duplicate", "future", "stale_date", "stale_flag",
                                    "code", "adjustment", "source", "evidence", "volume_unit", "price_scale", "bool", "nonfinite"])
def test_invalid_raw_history_blocks_all_picks(monkeypatch, damage):
    forbid_remote(monkeypatch)
    hist = history()
    if damage == "missing":
        histories = {}
    else:
        if damage == "ohlc":
            hist.loc[0, "open"] = 100
        elif damage == "volume":
            hist = hist.drop(columns="volume")
        elif damage == "short":
            hist = hist.iloc[1:].copy()
        elif damage == "duplicate":
            hist.loc[0, "date"] = hist.loc[1, "date"]
        elif damage == "future":
            hist.loc[60, "date"] = pd.Timestamp("2026-10-12")
        elif damage == "stale_date":
            hist["date"] = hist["date"] - pd.Timedelta(days=1)
        elif damage == "stale_flag":
            hist.attrs["stale"] = True
        elif damage == "price_scale":
            hist[["open", "high", "low", "close"]] *= 2
        elif damage == "bool":
            hist["volume"] = True
        elif damage == "nonfinite":
            hist.loc[0, "close"] = float("inf")
        else:
            hist.attrs[{"code": "code", "adjustment": "price_adjustment", "source": "daily_source",
                        "evidence": "evidence_id", "volume_unit": "volume_unit"}[damage]] = "wrong"
            if damage in {"source", "evidence"}:
                hist.attrs[{"source": "daily_source", "evidence": "evidence_id"}[damage]] = ""
        histories = {"600001": hist}
    result = preview(strategy="shrink_pullback", daily_histories=histories, technical_limit=1)
    assert result.result is None and result.readiness.state == "blocked"
    assert len(result.scope.technical_planned) == 2
    assert len(result.scope.technical_not_evaluated) == 0
    assert any("daily:600001" in item for item in result.readiness.blockers)


def test_budget_cannot_hide_missing_history_or_shrink_denominator():
    histories = {"600001": history(), "600002": pd.DataFrame(), "999999": pd.DataFrame()}
    result = preview(strategy="shrink_pullback", daily_histories=histories, technical_limit=1)
    assert result.result is None
    assert result.scope.technical_planned == ("600001", "600002")
    assert result.scope.technical_not_evaluated == ()
    assert result.readiness.valid_count < result.readiness.required_count


@pytest.mark.parametrize("info", list_strategies(Config().strategies_dir), ids=lambda info: info.name)
def test_all_ten_actual_yaml_previews(info):
    histories = {code: history(code) for code in ("600001", "600002")}
    result = preview(strategy=info.name, daily_histories=histories)
    assert result.result is not None
    assert result.scope is not None and not result.publish_allowed
    assert result.readiness.state == "degraded"
    assert result.readiness.valid_count == result.readiness.required_count
    assert any("not evaluated" in item for item in result.readiness.warnings)
    strategies = load_all_strategies(Config().strategies_dir)
    if strategies[info.name].screening.factor_weights.get("theme_heat", 0) > 0:
        assert any("neutral fallback" in item for item in result.readiness.warnings)


def test_calculation_exception_is_blocked_without_secret_text(monkeypatch):
    from src.services.research_delivery import screening_preview as adapter
    def fail(*a, **k):
        raise RuntimeError("secret-token-do-not-expose")
    monkeypatch.setattr(adapter, "screen", fail)
    result = preview(daily_histories={code: history(code) for code in ('600001', '600002')})
    assert result.result is None and result.readiness.state == "blocked"
    assert "calculation_failed" in result.readiness.blockers
    assert "secret-token" not in repr(result)


def test_one_stale_field_on_one_row_blocks_despite_current_batch_date(monkeypatch):
    from src.services.research_delivery import screening_preview as adapter
    monkeypatch.setattr(adapter, "screen", lambda *a, **k: pytest.fail("stale cell calculated"))
    frame = verified_snapshot()
    frame["price_date"] = [T, date(2026, 10, 8)]
    frame.attrs["field_trade_date_columns"]["price"] = "price_date"
    frame.attrs["max_trade_date"] = T
    result = preview(frame)
    assert "snapshot:600002:price:wrong_or_unknown_business_date" in result.readiness.blockers
    assert result.result is None


@pytest.mark.parametrize("valuation", [-1., 0.])
def test_nonpositive_valuation_is_real_evidence_not_missing(valuation):
    frame = verified_snapshot()
    frame["pe_ratio"] = valuation
    frame["pb_ratio"] = valuation
    result = preview(frame, daily_histories={code: history(code) for code in frame.code})
    assert result.readiness.valid_count == result.readiness.required_count
    assert result.readiness.state == "degraded" and result.result is not None


def test_failed_one_of_two_planned_histories_retains_full_denominator():
    histories = {"600001": history(), "600002": history("600002").drop(columns="volume")}
    result = preview(strategy="shrink_pullback", daily_histories=histories)
    assert result.scope.technical_planned == ("600001", "600002")
    assert not result.scope.technical_not_evaluated
    assert result.readiness.valid_count < result.readiness.required_count
    assert result.result is None


def test_mixed_defined_and_neutral_optional_rsi_does_not_block_valid_raw_data():
    second = history("600002")
    second.loc[second.index[:-1:2], ["open", "close"]] += .03
    second["high"] = second[["open", "close"]].max(axis=1) + .1
    result = preview(strategy="shrink_pullback", daily_histories={"600001": history(), "600002": second})
    assert result.result is not None
