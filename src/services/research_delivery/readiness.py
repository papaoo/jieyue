from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from src.services.research_delivery.contracts import (
    CellKey, DependencyManifest, FieldReceipt, FrozenScope, ReadinessResult,
)


def freeze_scope(
    manifest: DependencyManifest,
    universe: Sequence[str],
    snapshot_pass: Sequence[str],
    *,
    technical_limit: int = 100,
) -> FrozenScope:
    all_codes, passed = tuple(universe), tuple(snapshot_pass)
    if not all_codes or not manifest.snapshot_fields:
        raise ValueError("verified non-empty universe and snapshot requirements are required")
    if technical_limit < 1:
        raise ValueError("technical_limit must be positive")
    for values in (all_codes, passed):
        if len(set(values)) != len(values) or any(not code.strip() for code in values):
            raise ValueError("duplicate or empty code in scope")
    if not set(passed) <= set(all_codes):
        raise ValueError("snapshot candidates must belong to the frozen universe")
    # A work budget never removes mandatory evidence from the denominator.
    technical = passed if manifest.daily_fields else ()
    not_evaluated = ()
    keys = {
        CellKey("snapshot", code, field)
        for code in all_codes for field in manifest.snapshot_fields
    }
    keys.update(CellKey("daily", code, field) for code in technical for field in manifest.daily_fields)
    return FrozenScope(all_codes, passed, technical, not_evaluated, tuple(sorted(keys)))


def assess_readiness(
    scope: FrozenScope,
    receipts: Iterable[FieldReceipt],
    *,
    trade_date: date,
    calendar_verified: bool,
    universe_verified: bool,
    optional_gaps: Sequence[str] = (),
) -> ReadinessResult:
    blockers: list[str] = []
    warnings = tuple(dict.fromkeys(optional_gaps))
    if not calendar_verified:
        blockers.append("calendar_unknown")
    if not universe_verified or not scope.universe or not scope.required_cells:
        blockers.append("universe_or_requirements_unknown")
    indexed: dict[CellKey, FieldReceipt] = {}
    expected = set(scope.required_cells)
    for item in receipts:
        if item.key in indexed:
            raise ValueError("duplicate receipt")
        if item.key not in expected:
            raise ValueError("receipt outside declared scope")
        indexed[item.key] = item
    valid = not_applicable = 0
    for key in scope.required_cells:
        prefix = f"{key.stage}:{key.code}:{key.field}"
        item = indexed.get(key)
        if item is None:
            blockers.append(f"{prefix}:missing")
        elif item.status not in {"valid", "not_applicable"}:
            blockers.append(f"{prefix}:{item.status}")
        elif item.as_of != trade_date:
            blockers.append(f"{prefix}:wrong_or_unknown_business_date")
        elif not item.source.strip() or not item.evidence_id.strip():
            blockers.append(f"{prefix}:unproven")
        elif item.status == "not_applicable" and not item.reason_code.strip():
            blockers.append(f"{prefix}:unexplained_not_applicable")
        elif item.status == "not_applicable":
            not_applicable += 1
        else:
            valid += 1
    state = "blocked" if blockers else "degraded" if warnings else "ready"
    return ReadinessResult(state, len(scope.required_cells), valid, not_applicable,
                           tuple(blockers), warnings)
