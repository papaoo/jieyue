"""Strategy evidence semantics, using synthetic histories, never market returns."""
import pandas as pd
import pytest
from pathlib import Path

from src.services.screening.daily import compute_daily_features
from src.services.screening.models import ScreeningConfig
from src.services.screening.strategy import list_strategies
from tests.screening_reference_fixtures import canonical_frame


def history(close=10., ratio=1.):
    frame = pd.DataFrame(dict(close=[10.] * 61, open=[10.] * 61,
                              high=[10.] * 61, low=[9.] * 61, volume=[100.] * 61))
    frame.loc[60, "close"] = close
    frame.loc[60, "high"] = max(10., close)
    frame.loc[60, "volume"] = 100. * ratio
    return frame


@pytest.mark.parametrize("ratio,expected", [(0.99, True), (0.99999999, True), (1., False), (1.5, False)])
def test_volume_contraction_is_strict(ratio, expected):
    assert compute_daily_features(history(ratio=ratio))["volume_contracting"] is expected


@pytest.mark.parametrize("close,expected", [(9.99, False), (10., False), (10.00000001, True), (10.01, True)])
def test_breakout_is_strict_prior_high(close, expected):
    assert compute_daily_features(history(close=close))["breakout_confirmed"] is expected


@pytest.mark.parametrize("previous,today,expected", [(11., 10.5, True), (10.5, 11., False), (10., 10., False)])
def test_pullback_needs_falling_close_and_narrowing_distance(previous, today, expected):
    frame = history(close=today)
    frame.loc[59, "close"] = previous
    assert compute_daily_features(frame)["pullback_converging"] is expected


def test_falling_close_without_relative_distance_convergence_is_not_pullback():
    frame = history(close=9.9)
    frame.loc[40, "close"] = 100.
    assert compute_daily_features(frame)["pullback_converging"] is False


def test_catalog_declares_non_filter_daily_dependencies():
    capital = next(s for s in list_strategies(Path(__file__).resolve().parents[1] / "src/services/screening/strategies")
                   if s.name == "capital_heat")
    assert capital.requires_daily_features
    assert "signal_score" in capital.required_daily_fields
    assert capital.version == "2.1"
    assert all(s.version == "2.1" and s.requires_daily_features for s in
               list_strategies(Path(__file__).resolve().parents[1] / "src/services/screening/strategies"))


def test_legacy_custom_config_preserves_snapshot_only():
    from src.services.screening.requirements import requires_daily_evidence
    assert not requires_daily_evidence(ScreeningConfig())


@pytest.mark.parametrize("daily_filter", [False, True])
@pytest.mark.parametrize("new_observations", ["absent", "unknown"])
def test_actual_legacy_custom_strategy_accepts_historical_frozen_schema(tmp_path, daily_filter, new_observations):
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    path = tmp_path / "legacy.yaml"
    path.write_text("name: legacy_custom\nversion: '1.0'\nscreening:\n  enabled: true\n"
                    "  hard_filters:\n    exclude_st: false\n" +
                    ("    require_price_above_ma20: true\n" if daily_filter else ""), encoding="utf-8")
    frame, daily = frozen_inputs()
    historical = daily.drop(columns=["volume_contracting", "pullback_converging", "breakout_confirmed"])
    if new_observations == "unknown":
        for column in ("volume_contracting", "pullback_converging", "breakout_confirmed"):
            historical[column] = None
    result = screen("legacy_custom", config=Config(strategies_dir=tmp_path), frozen_snapshot=frame,
                    frozen_daily_features=historical if daily_filter else None, compute_only=True)
    assert len(result.picks) == 2
    assert result.strategy_version == "1.0"
    assert result.daily_enriched is daily_filter


@pytest.mark.parametrize("flag,column", [("require_volume_contraction", "volume_contracting"),
                                          ("require_pullback_convergence", "pullback_converging"),
                                          ("require_breakout_confirmation", "breakout_confirmed")])
def test_legacy_explicit_new_filter_requires_only_its_new_flag(tmp_path, flag, column):
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    (tmp_path / "legacy.yaml").write_text("name: legacy_custom\nscreening:\n  enabled: true\n"
                                          f"  hard_filters:\n    exclude_st: false\n    {flag}: true\n",
                                          encoding="utf-8")
    frame, daily = frozen_inputs()
    historical = daily.drop(columns=["volume_contracting", "pullback_converging", "breakout_confirmed"])
    config = Config(strategies_dir=tmp_path)
    with pytest.raises(ValueError, match="schema"):
        screen("legacy_custom", config=config, frozen_snapshot=frame,
               frozen_daily_features=historical, compute_only=True)
    historical[column] = True
    result = screen("legacy_custom", config=config, frozen_snapshot=frame,
                    frozen_daily_features=historical, compute_only=True)
    assert len(result.picks) == 2
    historical[column] = None
    with pytest.raises(ValueError, match="boolean"):
        screen("legacy_custom", config=config, frozen_snapshot=frame,
               frozen_daily_features=historical, compute_only=True)


@pytest.mark.parametrize("column", ["volume_contracting", "pullback_converging", "breakout_confirmed"])
def test_daily_required_still_requires_every_new_flag(column):
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    frame, daily = frozen_inputs()
    with pytest.raises(ValueError, match="schema"):
        screen("capital_heat", config=Config(), frozen_snapshot=frame,
               frozen_daily_features=daily.drop(columns=column), compute_only=True)


def test_daily_required_rejects_missing_frozen_evidence():
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    frame = pd.DataFrame([dict(code="600001", name="示例", price=10., amount=1e9,
                              total_mv=1e10, pe_ratio=8., pb_ratio=1.,
                              turnover_rate=3., volume_ratio=2., change_pct=2.)])
    frame = canonical_frame(frame)
    with pytest.raises(ValueError, match="daily"):
        screen("capital_heat", config=Config(), frozen_snapshot=frame, compute_only=True)


def frozen_inputs():
    codes = ["600001", "600002"]
    frame = pd.DataFrame([dict(code=code, name="示例", price=10., amount=1e9,
                              total_mv=1e10, pe_ratio=8., pb_ratio=1.,
                              turnover_rate=3., volume_ratio=2., change_pct=2.) for code in codes])
    daily = pd.DataFrame([dict(code=code, daily_source="fixture", **compute_daily_features(history())) for code in codes])
    return canonical_frame(frame), daily


def test_strict_frozen_ignores_enrichment_budget_and_requires_every_row():
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    frame, daily = frozen_inputs()
    result = screen("capital_heat", config=Config(), frozen_snapshot=frame, frozen_daily_features=daily,
                    compute_only=True, daily_enrich_max_candidates=1)
    assert result.daily_enrich_count == 2
    with pytest.raises(ValueError, match="missing frozen"):
        screen("capital_heat", config=Config(), frozen_snapshot=frame, frozen_daily_features=daily.iloc[:1],
               compute_only=True, daily_enrich_max_candidates=1)


def test_strict_normal_budget_failure_precedes_daily_fetch(monkeypatch):
    from src.services.screening import pipeline
    from src.services.screening.config import Config
    frame, _ = frozen_inputs()
    def forbidden(*args, **kwargs):
        pytest.fail("budget-incomplete strategy must not fetch a subset")
    monkeypatch.setattr(pipeline, "enrich_daily_features", forbidden)
    with pytest.raises(RuntimeError, match="budget"):
        pipeline.screen("capital_heat", config=Config(), frozen_snapshot=frame,
                        use_llm=False, daily_enrich_max_candidates=1)


def test_compute_only_keeps_below_llm_cutoff_until_real_risk_overlay(monkeypatch):
    from src.services.screening import pipeline
    from src.services.screening.config import Config
    frame, daily = frozen_inputs()
    frame.loc[0, "volume_ratio"] = 10.
    def scores(df, config):
        return df.assign(screen_score=df["code"].map({"600001": 80., "600002": 79.}))
    monkeypatch.setattr(pipeline, "compute_screen_scores", scores)
    result = pipeline.screen("capital_heat", config=Config(llm_max_candidates=1, post_analyzers=[]),
                             frozen_snapshot=frame, frozen_daily_features=daily, compute_only=True, max_output=1)
    assert result.picks[0].code == "600002"


@pytest.mark.parametrize("column", ["volume_contracting", "pullback_converging", "breakout_confirmed"])
@pytest.mark.parametrize("invalid", [1, "true", None])
def test_frozen_flags_reject_non_boolean_observations(column, invalid):
    from src.services.screening.pipeline import _merge_frozen_daily_features
    frame, daily = frozen_inputs()
    daily[column] = invalid
    with pytest.raises(ValueError, match="boolean"):
        _merge_frozen_daily_features(frame, daily, screening=ScreeningConfig(evidence_mode="daily_required"))


@pytest.mark.parametrize("flag,column", [("require_volume_contraction", "volume_contracting"),
                                          ("require_pullback_convergence", "pullback_converging"),
                                          ("require_breakout_confirmation", "breakout_confirmed")])
def test_boolean_filters_and_diagnostics_preserve_false_vs_missing(flag, column):
    from src.services.screening.filter import apply_hard_filters, check_hard_filters, hard_filter_waterfall
    from src.services.screening.models import HardFilterConfig
    filters = HardFilterConfig(exclude_st=False, **{flag: True})
    checks = check_hard_filters({column: False}, filters)
    assert checks[0]["status"] == "fail" and checks[0]["current_value"] is False
    assert check_hard_filters({"code": "600001"}, filters)[0]["status"] == "missing"
    frame = pd.DataFrame([{column: True}, {column: False}])
    assert len(apply_hard_filters(frame, filters)) == 1
    assert hard_filter_waterfall(frame, filters)


def test_normal_strict_missing_fields_do_not_become_neutral_scores(monkeypatch):
    from src.services.screening import pipeline
    from src.services.screening.config import Config
    frame, _ = frozen_inputs()
    monkeypatch.setattr(pipeline, "enrich_daily_features", lambda candidates, **kwargs: candidates.copy())
    with pytest.raises(RuntimeError, match="required.*failed"):
        pipeline.screen("capital_heat", config=Config(), frozen_snapshot=frame, use_llm=False)


def test_missing_raw_high_is_unknown_not_synthesized_breakout():
    frame = history(close=11.).drop(columns="high")
    assert pd.isna(compute_daily_features(frame)["breakout_confirmed"])


def test_incomplete_raw_high_is_unknown_not_filled_breakout():
    frame = history(close=11.)
    frame.loc[50, "high"] = float("nan")
    assert pd.isna(compute_daily_features(frame)["breakout_confirmed"])


def test_missing_volume_is_unknown_not_false():
    assert pd.isna(compute_daily_features(history().drop(columns="volume"))["volume_contracting"])


def test_scorecard_renames_emitted_tags_but_keeps_legacy_profile_keys():
    from src.services.screening.post_analysis import _scorecard_delta
    from src.services.screening.models import Pick
    pick = Pick(rank=1, code="600001", name="示例", final_score=80., screen_score=80.,
                factor_scores={"value": 80., "stability": 80., "momentum": 80., "activity": 80.})
    delta, _, tags, summary = _scorecard_delta(pick, profile={"value_quality_bonus": 1., "capital_confirmed_bonus": 2.})
    assert delta == 3.
    assert tags == ["valuation_stability", "price_volume_active"]
    assert "value_quality" not in summary and "capital_confirmed" not in summary


@pytest.mark.parametrize("mode", ["invalid", None, []])
def test_evidence_mode_rejects_unknown_values(mode):
    with pytest.raises(ValueError, match="evidence_mode"):
        ScreeningConfig(evidence_mode=mode)


def test_raw_numeric_truth_is_not_verified_boolean_evidence():
    from src.services.screening.filter import apply_hard_filters, check_hard_filters
    from src.services.screening.models import HardFilterConfig
    filters = HardFilterConfig(exclude_st=False, require_volume_contraction=True)
    assert apply_hard_filters(pd.DataFrame([{"volume_contracting": 1}]), filters).empty
    check = check_hard_filters({"volume_contracting": 1}, filters)[0]
    assert check["status"] == "missing" and check["missing_reason"] == "invalid_boolean"
