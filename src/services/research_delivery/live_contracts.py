"""Shared internal producer payload; no caller-supplied publication authority."""
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import math
from typing import Literal

import numpy as np


def json_safe(value):
    """Normalize known scalar types, never stringify arbitrary upstream objects."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("producer_json_key_invalid")
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    raise ValueError("producer_json_value_invalid")


@dataclass(frozen=True)
class ProducerResult:
    category_id: str
    business_date: str
    state: Literal["ready", "degraded", "blocked", "skipped"]
    strategy_version: str = ""
    result: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    def to_dict(self):
        return json_safe(asdict(self))
