from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


FieldStatus = Literal["valid", "not_applicable", "missing", "denied", "timeout", "invalid"]


@dataclass(frozen=True)
class DependencyManifest:
    strategy_id: str
    strategy_version: str
    snapshot_fields: tuple[str, ...]
    daily_fields: tuple[str, ...]


@dataclass(frozen=True, order=True)
class CellKey:
    stage: Literal["snapshot", "daily"]
    code: str
    field: str


@dataclass(frozen=True)
class FieldReceipt:
    key: CellKey
    status: FieldStatus
    as_of: date | None
    source: str = ""
    evidence_id: str = ""
    reason_code: str = ""


@dataclass(frozen=True)
class FrozenScope:
    universe: tuple[str, ...]
    snapshot_pass: tuple[str, ...]
    technical_planned: tuple[str, ...]
    technical_not_evaluated: tuple[str, ...]
    required_cells: tuple[CellKey, ...]


@dataclass(frozen=True)
class ReadinessResult:
    state: Literal["ready", "degraded", "blocked"]
    required_count: int
    valid_count: int
    not_applicable_count: int
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
