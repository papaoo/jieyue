"""Request-local offline execution contracts (synthetic input, not market evidence)."""
import copy

import pandas as pd
import pytest

from src.services.screening import pipeline
from src.services.screening.config import Config
from src.services.screening.daily import compute_daily_features
from tests.screening_reference_fixtures import canonical_frame


def snapshot(codes=("600001", "600002")):
    return canonical_frame(pd.DataFrame([dict(code=code, name="示例", price=10., amount=1e9,
                              total_mv=1e10, pe_ratio=8., pb_ratio=1.,
                              turnover_rate=2., volume_ratio=1.2, change_pct=1.)
                         for code in codes], index=[7] * len(codes)))


def features(codes=("600001", "600002")):
    close = pd.Series([9 + i / 60 for i in range(61)])
    hist = pd.DataFrame(dict(date=pd.date_range("2026-07-01", periods=61),
                             open=close, high=close + .1, low=close - .1,
                             close=close, volume=1e6))
    return pd.DataFrame([dict(code=code, daily_source="fixture",
                              **compute_daily_features(hist)) for code in codes], index=[9] * len(codes))


def forbid_remote(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external path executed")
    for name in ("fetch_snapshot_with_fallback", "enrich_daily_features",
                 "enrich_industry_concepts", "apply_dsa_provider_context",
                 "collect_candidate_context", "build_llm_context", "rank_candidates_with_metadata"):
        monkeypatch.setattr(pipeline, name, forbidden)
    monkeypatch.setattr(Config, "from_env", forbidden)
    from src.services.screening import post_analysis, daily
    for name in ("_run_dsa_analyzer", "_run_external_http_analyzer"):
        monkeypatch.setattr(post_analysis, name, forbidden)
    monkeypatch.setattr(daily, "fetch_daily_history", forbidden)
    return forbidden


def test_compute_only_real_rule_risk_scorecard_no_remote_or_mutation(monkeypatch):
    forbidden = forbid_remote(monkeypatch)
    config = Config(llm_api_key="dummy", industry_provider="akshare",
                    industry_map_files=["missing.csv"], post_analyzers=["scorecard", "dsa", "external_http"],
                    dsa_notify=True, daily_enrich_enabled=False)
    original_config = copy.deepcopy(config)
    frame = snapshot()
    frame.attrs["nested"] = {"values": [1]}
    original = copy.deepcopy(frame)
    result = pipeline.screen("dual_low", config=config, frozen_snapshot=frame, compute_only=True,
                             frozen_daily_features=features(),
                             deep_analysis=True, use_llm=True, collect_llm_candidate_context=True,
                             context={"dsa": {"quote": forbidden}}, industry_map_files=["missing.csv"],
                             selection_seed="must-not-rotate", post_analyzers=["scorecard", "dsa"])
    assert len(result.picks) == 2
    assert result.risk_enabled and not result.llm_ranked
    assert result.post_analyzers == ["scorecard"]
    assert all(p.factor_scores and p.post_analysis_status.get("scorecard") == "completed" for p in result.picks)
    assert not result.result_variant_applied
    assert config == original_config
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original.attrs


@pytest.mark.parametrize("kwargs", [{}, {"config": Config()}])
def test_compute_only_requires_explicit_inputs_before_provider(monkeypatch, kwargs):
    forbid_remote(monkeypatch)
    with pytest.raises(ValueError):
        pipeline.screen("dual_low", compute_only=True, **kwargs)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "wrong_code", "missing_column", "quote_collision", "invalid"])
def test_compute_only_rejects_bad_daily_features(monkeypatch, damage):
    forbid_remote(monkeypatch)
    frozen = features()
    if damage == "missing":
        frozen = None
    elif damage == "duplicate":
        frozen.loc[9, "code"] = "600001"
    elif damage == "wrong_code":
        frozen.iloc[0, frozen.columns.get_loc("code")] = "600003"
    elif damage == "missing_column":
        frozen = frozen.drop(columns="signal_score")
    elif damage == "quote_collision":
        frozen["price"] = 99
    else:
        frozen["signal_score"] = float("inf")
    with pytest.raises((ValueError, RuntimeError)):
        pipeline.screen("shrink_pullback", config=Config(), compute_only=True,
                        frozen_snapshot=snapshot(), frozen_daily_features=frozen)


@pytest.mark.parametrize("strategy,requested", [("shrink_pullback", False), ("dual_low", True)])
def test_frozen_features_align_by_code_not_index(monkeypatch, strategy, requested):
    forbid_remote(monkeypatch)
    frame, frozen = snapshot(), features().iloc[::-1].copy()
    result = pipeline.screen(strategy, config=Config(), frozen_snapshot=frame,
                             frozen_daily_features=frozen, compute_only=True, daily_enrich=requested)
    assert result.daily_enriched and result.daily_enrich_count == 2
    assert all(p.price == 10 for p in result.picks)


def test_legacy_normal_mode_still_fetches_snapshot(monkeypatch, tmp_path):
    (tmp_path / 'legacy.yaml').write_text('name: legacy\nscreening:\n  enabled: true\n', encoding='utf-8')
    calls = []
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: calls.append(k) or snapshot())
    monkeypatch.setattr(pipeline, "enrich_daily_features",
                        lambda frame, **kwargs: pipeline._merge_frozen_daily_features(frame, features()))
    result = pipeline.screen("legacy", config=Config(strategies_dir=tmp_path), use_llm=False)
    assert calls and result.picks


def test_empty_frozen_daily_strategy_does_not_need_daily_input(monkeypatch):
    forbid_remote(monkeypatch)
    frame = snapshot()
    frame["amount"] = 0
    result = pipeline.screen("shrink_pullback", config=Config(), compute_only=True, frozen_snapshot=frame)
    assert not result.picks and not result.daily_enriched


def test_actual_risk_and_scorecard_keep_final_adjusted_scores(monkeypatch):
    forbid_remote(monkeypatch)
    frame = snapshot()
    frame.iloc[0, frame.columns.get_loc("volume_ratio")] = 10.
    result = pipeline.screen("dual_low", config=Config(), compute_only=True, frozen_snapshot=frame,
                             frozen_daily_features=features())
    risky = next(p for p in result.picks if p.code == "600001")
    assert risky.risk_flags and risky.risk_penalty > 0
    assert risky.post_analysis_score_deltas["scorecard"] != 0
    assert risky.final_score != risky.screen_score


@pytest.mark.parametrize("cap", [0, -1, True])
def test_frozen_invalid_cap_cannot_reinterpret_default(monkeypatch, cap):
    forbid_remote(monkeypatch)
    with pytest.raises(ValueError):
        pipeline.screen("shrink_pullback", config=Config(), compute_only=True,
                        frozen_snapshot=snapshot(), daily_enrich_max_candidates=cap)
