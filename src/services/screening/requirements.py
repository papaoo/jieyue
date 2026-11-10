# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Shared strategy evidence contract for catalog and runtime execution."""
from src.services.screening.daily import _DAILY_FEATURE_DEFAULTS
from src.services.screening.models import ScreeningConfig


def required_daily_fields(config: ScreeningConfig) -> tuple[str, ...]:
    # Keep the established filter mapping authoritative for legacy strategies.
    from src.services.screening.strategy import _required_daily_fields
    if config.evidence_mode == "daily_required":
        # The shared scoring/risk/scorecard implementation consumes this complete
        # technical record, even when no daily hard filter is configured.
        return tuple(_DAILY_FEATURE_DEFAULTS)
    return tuple(_required_daily_fields(config.hard_filters))


def requires_daily_evidence(config: ScreeningConfig) -> bool:
    return bool(required_daily_fields(config))
