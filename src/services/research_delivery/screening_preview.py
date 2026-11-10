"""Internal A-share frozen preview, never a publication or production collector.

Snapshot metadata asserts CNY/share, CNY amounts/capitalization, percentage
points, dimensionless ratios and TTM PE. History metadata asserts forward
adjustment and shares. These labels are not evidence of collector authenticity.
61 bars provide minimum MA60/60-session-return warmup, not full MACD convergence
or proof of contiguous exchange sessions. Caller supplies closed-session inputs.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

import pandas as pd

from src.services.research_delivery.contracts import CellKey, FieldReceipt, FrozenScope, ReadinessResult
from src.services.research_delivery.dependencies import build_manifest
from src.services.research_delivery.evidence import CORE_FIELDS, canonical_code, validate_history, validate_snapshot
from src.services.research_delivery.readiness import assess_readiness, freeze_scope
from src.services.screening.config import Config
from src.services.screening.daily import _DAILY_FEATURE_DEFAULTS, compute_daily_features, strict_history_window
from src.services.screening.filter import apply_hard_filters, without_daily_filters
from src.services.screening.models import ScreenResult
from src.services.screening.pipeline import _merge_frozen_daily_features, _provisional_candidates, screen
from src.services.screening.scorer import normalized_factor_weights
from src.services.screening.strategy import list_strategies, load_all_strategies


@dataclass(frozen=True)
class StrategyPreview:
    readiness: ReadinessResult
    scope: FrozenScope | None
    result: ScreenResult | None
    publish_allowed: bool = False


def _add_blockers(readiness: ReadinessResult, blockers: Sequence[str]) -> ReadinessResult:
    if not blockers:
        return readiness
    return replace(readiness, state="blocked", blockers=tuple(dict.fromkeys((*readiness.blockers, *blockers))))


def preview_strategy(
    strategy: str, snapshot: pd.DataFrame, *, trade_date: date, universe: Sequence[str],
    calendar_verified: bool, universe_verified: bool,
    daily_histories: Mapping[str, pd.DataFrame] | None = None,
    technical_limit: int = 100, config: Config | None = None, max_output: int | None = None,
    feature_cache: dict | None = None,
) -> StrategyPreview:
    """Validate frozen evidence then compute the real rule/risk/scorecard result.

    No env, wall-clock/calendar, provider, files for enrichment, LLM, publishing,
    credentials, approval or send-window checks occur in this adapter.
    """
    if not isinstance(snapshot, pd.DataFrame) or not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise TypeError("snapshot DataFrame and business date are required")
    if isinstance(technical_limit, bool) or not isinstance(technical_limit, int) or technical_limit < 1:
        raise ValueError("technical_limit must be a positive integer")
    if max_output is not None and (isinstance(max_output, bool) or not isinstance(max_output, int) or not 1 <= max_output <= 20):
        raise ValueError("max_output must be an integer between 1 and 20")
    if not isinstance(calendar_verified, bool) or not isinstance(universe_verified, bool):
        raise TypeError("verification flags must be bool")
    runtime = config if config is not None else Config()
    strategies = load_all_strategies(runtime.strategies_dir)
    if strategy not in strategies:
        raise ValueError("unknown strategy")
    strat = strategies[strategy]
    if "cn" not in strat.screening.market_scope:
        raise ValueError("preview supports A-shares only")
    info = next(item for item in list_strategies(runtime.strategies_dir) if item.name == strategy)
    manifest = build_manifest(info)
    # The legacy strategy metadata covers hard filters. Strict delivery also
    # covers snapshot inputs consumed by rule scores, risks and output facts.
    manifest = replace(manifest, snapshot_fields=tuple(sorted(set((*manifest.snapshot_fields, *CORE_FIELDS)))))
    if manifest.daily_fields:
        manifest = replace(manifest, daily_fields=tuple(sorted(set((*manifest.daily_fields,
                            *_DAILY_FEATURE_DEFAULTS)))))
    optional_gaps = [
        "Board heat, news and fundamental context not evaluated; industry grouping uses supplied canonical reference metadata when valid.",
        "Structural preview does not independently authenticate supplied source/unit/reference assertions; original responses and exchange-session coverage require adapter verification.",
    ]
    weights = normalized_factor_weights(strat.screening)
    for factor in ("theme_heat", "topic_alignment"):
        if weights.get(factor, 0) > 0:
            optional_gaps.append(f"{factor}: unsupported optional evidence not evaluated; existing neutral fallback used.")
    if manifest.daily_fields:
        optional_gaps.append("61-bar structural minimum does not prove full MACD convergence or exchange-session continuity; adapter history-window and session checks are separate guarantees.")
    else:
        optional_gaps.append("Optional daily refinements not evaluated; snapshot-only scoring branch used.")
    codes = tuple(universe) if not isinstance(universe, (str, bytes)) else ()
    if not codes or any(not canonical_code(code) for code in codes) or len(set(codes)) != len(codes):
        return StrategyPreview(ReadinessResult("blocked", 0, 0, 0, ("universe_invalid",), tuple(optional_gaps)), None, None)
    scope = freeze_scope(manifest, codes, (), technical_limit=technical_limit)
    if not calendar_verified or not universe_verified:
        readiness = assess_readiness(scope, (), trade_date=trade_date, calendar_verified=calendar_verified,
                                     universe_verified=universe_verified, optional_gaps=optional_gaps)
        return StrategyPreview(readiness, scope, None)
    accepted, receipts, blockers = validate_snapshot(snapshot, manifest.snapshot_fields, codes, trade_date)
    if strat.screening.evidence_mode == 'daily_required' and accepted is not None:
        from src.services.screening.industry_reference import industry_identity
        if any(not industry_identity(row, trade_date).startswith('capco_major_v1:') for _, row in accepted.iterrows()):
            blockers.append('required_published_capco_reference_missing')
    readiness = _add_blockers(assess_readiness(scope, receipts, trade_date=trade_date,
                              calendar_verified=True, universe_verified=True, optional_gaps=optional_gaps), blockers)
    if readiness.state == "blocked":
        return StrategyPreview(readiness, scope, None)
    features = None
    try:
        snapshot_filters = without_daily_filters(strat.screening.hard_filters)
        provisional = _provisional_candidates(apply_hard_filters(accepted, snapshot_filters), strat.screening)
        scope = freeze_scope(manifest, codes, provisional["code"].tolist(), technical_limit=technical_limit)
        daily_rows = []
        for code in scope.technical_planned:
            daily_receipts = []
            try:
                supplied = (daily_histories or {}).get(code)
                if strat.screening.evidence_mode == 'daily_required' and isinstance(supplied, pd.DataFrame):
                    supplied = strict_history_window(supplied, trade_date)
                raw = validate_history(supplied, code, trade_date,
                                       float(accepted.loc[accepted["code"] == code, "price"].iloc[0]))
                # Cache only after full chronology/provenance validation; the
                # real pipeline schema guard below still runs for each strategy.
                from src.services.research_delivery.collection_checkpoint import digest, pack_frame
                cache_key = digest(dict(code=code, date=trade_date.isoformat(), history=pack_frame(raw)))
                values = feature_cache.get(cache_key) if feature_cache is not None else None
                if values is None:
                    values = compute_daily_features(raw)
                    if feature_cache is not None:
                        feature_cache[cache_key] = dict(values)
                row = dict(code=code, **values, daily_source=raw.attrs["daily_source"])
                candidate = provisional.loc[provisional["code"] == code]
                # Same schema/derived-value guard used by the actual pipeline.
                _merge_frozen_daily_features(candidate, pd.DataFrame([row]), screening=strat.screening)
                daily_rows.append(row)
                daily_receipts = [FieldReceipt(CellKey("daily", code, field), "valid", trade_date,
                                 raw.attrs["daily_source"], raw.attrs["evidence_id"]) for field in manifest.daily_fields]
            except Exception:
                # Never surface arbitrary upstream error text; failed code keeps
                # every originally planned dependency in the denominator.
                daily_receipts = [FieldReceipt(CellKey("daily", code, field), "invalid", trade_date)
                                  for field in manifest.daily_fields]
            receipts.extend(daily_receipts)
        if scope.technical_not_evaluated:
            optional_gaps.append(f"technical_not_evaluated: {len(scope.technical_not_evaluated)} candidates outside cap.")
        readiness = assess_readiness(scope, receipts, trade_date=trade_date, calendar_verified=True,
                                     universe_verified=True, optional_gaps=optional_gaps)
        if readiness.state == "blocked":
            return StrategyPreview(readiness, scope, None)
        if daily_rows:
            features = pd.DataFrame(daily_rows)
        result = screen(strategy, config=runtime, market="cn", max_output=max_output, frozen_snapshot=accepted,
                        frozen_daily_features=features, compute_only=True, daily_enrich=False,
                        daily_enrich_max_candidates=technical_limit, use_llm=False,
                        post_analyzers=["scorecard"], deep_analysis=False, selection_seed="")
        result.degradation.extend(readiness.warnings)
        return StrategyPreview(readiness, scope, result)
    except Exception:
        return StrategyPreview(_add_blockers(readiness, ("calculation_failed",)), scope, None)
