from dataclasses import replace
from pathlib import Path

import pytest

from src.services.research_delivery.dependencies import build_manifest
from src.services.screening.strategy import list_strategies


STRATEGIES = Path(__file__).resolve().parents[1] / "src/services/screening/strategies"


def test_all_ten_strategy_manifests_reuse_existing_fields():
    infos = list_strategies(STRATEGIES)
    assert len(infos) == 10
    for info in infos:
        manifest = build_manifest(info)
        assert manifest.strategy_id == info.name
        assert manifest.strategy_version == info.version
        assert manifest.snapshot_fields == tuple(sorted(set(info.required_snapshot_fields)))
        assert manifest.daily_fields == tuple(sorted(set(info.required_daily_fields)))


def test_all_strict_strategies_declare_daily_scoring_dependencies():
    manifests = {info.name: build_manifest(info) for info in list_strategies(STRATEGIES)}
    assert "signal_score" in manifests["volume_breakout"].daily_fields
    assert 'signal_score' in manifests["dual_low"].daily_fields
    assert {"pe_ratio", "pb_ratio"} <= set(manifests["dual_low"].snapshot_fields)


def test_missing_identity_or_dependency_is_rejected():
    info = list_strategies(STRATEGIES)[0]
    for changed in (
        replace(info, name=""),
        replace(info, version=""),
        replace(info, required_snapshot_fields=[]),
    ):
        with pytest.raises(ValueError):
            build_manifest(changed)
