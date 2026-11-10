from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.services.research_delivery.contracts import DependencyManifest, FieldReceipt
from src.services.research_delivery.readiness import assess_readiness, freeze_scope


DAY = date(2026, 10, 13)
MANIFEST = DependencyManifest("fixture", "1", ("price",), ("ma20",))


def scope():
    return freeze_scope(MANIFEST, ("A", "B", "C"), ("B", "A"), technical_limit=1)


def receipts(frozen):
    return tuple(FieldReceipt(key, "valid", DAY, "fixture", f"proof-{i}")
                 for i, key in enumerate(frozen.required_cells))


def assess(frozen, items, **overrides):
    options = dict(trade_date=DAY, calendar_verified=True, universe_verified=True)
    options.update(overrides)
    return assess_readiness(frozen, items, **options)


def test_budget_does_not_change_required_evidence_or_universe():
    frozen = scope()
    assert frozen.technical_planned == ("B", "A")
    assert frozen.technical_not_evaluated == ()
    assert frozen.universe == ("A", "B", "C")
    assert len(frozen.required_cells) == 5
    result = assess(frozen, receipts(frozen))
    assert result.state == "ready"
    assert result.valid_count == result.required_count == 5


@pytest.mark.parametrize("status", ["missing", "denied", "timeout", "invalid"])
def test_missing_mandatory_data_blocks(status):
    frozen = scope()
    items = list(receipts(frozen))
    items[-1] = replace(items[-1], status=status)
    result = assess(frozen, items)
    assert result.state == "blocked"
    assert any(status in reason for reason in result.blockers)


def test_stale_unknown_date_and_absent_receipt_block():
    frozen = scope()
    good = receipts(frozen)
    for bad_date in (None, DAY - timedelta(days=1), DAY + timedelta(days=1)):
        items = (replace(good[0], as_of=bad_date), *good[1:])
        assert assess(frozen, items).state == "blocked"
    assert assess(frozen, good[:-1]).state == "blocked"


def test_no_source_or_evidence_is_not_valid():
    frozen = scope()
    good = receipts(frozen)
    for change in ({"source": ""}, {"evidence_id": ""}):
        bad = (replace(good[0], **change), *good[1:])
        assert assess(frozen, bad).state == "blocked"


def test_unknown_calendar_or_universe_blocks_even_with_receipts():
    frozen = scope()
    for change in ({"calendar_verified": False}, {"universe_verified": False}):
        assert assess(frozen, receipts(frozen), **change).state == "blocked"


def test_not_applicable_requires_reason_and_remains_visible():
    frozen = scope()
    good = receipts(frozen)
    item = replace(good[0], status="not_applicable")
    assert assess(frozen, (item, *good[1:])).state == "blocked"
    item = replace(item, reason_code="confirmed_suspended")
    result = assess(frozen, (item, *good[1:]))
    assert result.state == "ready"
    assert result.not_applicable_count == 1
    assert result.valid_count == 4


def test_optional_gaps_degrade_but_do_not_erase_core_blockers():
    frozen = scope()
    gaps = ("announcement_not_verified",)
    result = assess(frozen, receipts(frozen), optional_gaps=gaps)
    assert result.state == "degraded" and result.warnings == gaps
    assert assess(frozen, (), optional_gaps=gaps).state == "blocked"


def test_zero_snapshot_candidates_still_requires_full_snapshot():
    frozen = freeze_scope(MANIFEST, ("A", "B"), ())
    assert frozen.technical_planned == ()
    assert assess(frozen, receipts(frozen)).state == "ready"
    assert assess(frozen, ()).state == "blocked"


def test_duplicate_receipts_and_invalid_scope_are_rejected():
    frozen = scope()
    good = receipts(frozen)
    with pytest.raises(ValueError, match="duplicate"):
        assess(frozen, (*good, good[0]))
    for universe, candidates in (((), ()), (("A", "A"), ()), (("A",), ("B",))):
        with pytest.raises(ValueError):
            freeze_scope(MANIFEST, universe, candidates)


def test_out_of_scope_receipt_is_rejected():
    frozen = scope()
    good = receipts(frozen)
    outside = replace(good[0], key=replace(good[0].key, code="OUTSIDE"))
    with pytest.raises(ValueError, match="outside declared scope"):
        assess(frozen, (*good, outside))
