from src.services.research_delivery.contracts import DependencyManifest
from src.services.screening.models import StrategyInfo


def build_manifest(info: StrategyInfo) -> DependencyManifest:
    if not info.name.strip() or not info.version.strip():
        raise ValueError("strategy identity and version are required")
    snapshot = tuple(sorted(set(info.required_snapshot_fields)))
    daily = tuple(sorted(set(info.required_daily_fields)))
    if not snapshot or any(not field.strip() for field in (*snapshot, *daily)):
        raise ValueError("non-empty snapshot dependencies are required")
    if info.requires_daily_features and not daily:
        raise ValueError("daily strategy has no declared daily dependencies")
    return DependencyManifest(info.name, info.version, snapshot, daily)
