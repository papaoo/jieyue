"""Pure diagnostics for captured Tushare tables, not a collector or publisher.

The digest identifies scoped input values and normalization semantics; it does
not authenticate the source or persist original responses. stock_basic.name
has no verified business date and is deliberately left blocked.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
import json
import re

import numpy as np
import pandas as pd

from src.services.research_delivery.contracts import DependencyManifest, ReadinessResult
from src.services.research_delivery.evidence import CORE_FIELDS, canonical_code, validate_snapshot
from src.services.research_delivery.readiness import assess_readiness, freeze_scope
from src.services.screening.snapshot import _prepare_tushare_snapshot


_NORMALIZATION_VERSION = "tushare_snapshot_ttm_cny_v1_strict_outer"
_RAW_COLUMNS = (
    ("ts_code", "symbol", "trade_date", "close", "pct_chg", "amount"),
    ("ts_code", "symbol", "trade_date", "turnover_rate", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv"),
    ("ts_code", "symbol", "name", "industry"),
)


def _scalar(value: object) -> list:
    """Stable typed encoding, never arbitrary str/repr or object serialization."""
    if value is None or value is pd.NA or value is pd.NaT:
        return ["null"]
    # NumPy durations inherit np.integer but are not numeric observations.
    if isinstance(value, np.timedelta64):
        raise ValueError("tushare_source_scalar_invalid")
    if isinstance(value, (bool, np.bool_)):
        return ["bool", bool(value)]
    if isinstance(value, (int, np.integer)):
        return ["int", int(value)]
    if isinstance(value, (float, np.floating)):
        numeric = float(value)
        return ["float", numeric.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    raise ValueError("tushare_source_scalar_invalid")


def _validate_sources(tables: tuple[pd.DataFrame | None, ...]) -> None:
    identities: dict[str, str] = {}
    for index, table in enumerate(tables):
        if table is None and index == 2:
            continue
        if (not isinstance(table, pd.DataFrame) or not table.columns.is_unique
                or any(not isinstance(column, str) for column in table.columns)):
            raise ValueError("tushare_source_schema_invalid")
        # Check all raw scalars before selecting endpoint fields or universe.
        for row in table.itertuples(index=False, name=None):
            for value in row:
                _scalar(value)
        if table.empty and index == 2:
            continue
        if "ts_code" not in table:
            raise ValueError("tushare_source_schema_invalid")
        symbols = table["symbol"].tolist() if "symbol" in table else [None] * len(table)
        seen = set()
        for ts_code, symbol in zip(table["ts_code"], symbols):
            if (not isinstance(ts_code, str) or re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", ts_code) is None
                    or ts_code in seen):
                raise ValueError("tushare_source_schema_invalid")
            seen.add(ts_code)
            code = ts_code[:6]
            if code in identities and identities[code] != ts_code:
                raise ValueError("tushare_source_schema_invalid")
            identities[code] = ts_code
            if not pd.isna(symbol) and symbol != code:
                raise ValueError("tushare_source_schema_invalid")


def _evidence_id(tables: tuple[pd.DataFrame | None, ...]) -> str:
    payload = [_NORMALIZATION_VERSION]
    for label, table, owned in zip(("daily", "daily_basic", "stock_basic"), tables, _RAW_COLUMNS):
        if table is None:
            payload.append([label, None])
            continue
        columns = [column for column in owned if column in table]
        ordered = table.sort_values("ts_code") if "ts_code" in table else table
        rows = [[_scalar(value) for value in row]
                for row in ordered.loc[:, columns].itertuples(index=False, name=None)]
        payload.append([label, columns, rows])
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def assess_tushare_snapshot(
    daily: pd.DataFrame,
    daily_basic: pd.DataFrame,
    stock_basic: pd.DataFrame | None,
    *,
    trade_date: date,
    universe: Sequence[str],
    calendar_verified: bool,
    universe_verified: bool,
) -> ReadinessResult:
    """Assess all core cells in an independent universe; never fetch or publish.

    Schema/type errors raise fixed ValueError codes. Missing or invalid numeric
    observations remain field blockers. Verification flags are caller assertions,
    never inferred from returned rows or timestamps.
    """
    if (not isinstance(trade_date, date) or isinstance(trade_date, datetime)
            or not isinstance(calendar_verified, bool) or not isinstance(universe_verified, bool)):
        raise ValueError("tushare_request_invalid")
    if not isinstance(universe, Sequence) or isinstance(universe, (str, bytes)):
        raise ValueError("tushare_universe_invalid")
    codes = tuple(universe)
    if not codes or any(not canonical_code(code) for code in codes) or len(set(codes)) != len(codes):
        raise ValueError("tushare_universe_invalid")
    tables = (daily, daily_basic, stock_basic)
    _validate_sources(tables)
    selected = tuple(None if table is None else table.loc[
        table["ts_code"].map(lambda value: value[:6]).isin(codes)
    ].copy() if "ts_code" in table else table.copy() for table in tables)
    evidence_id = _evidence_id(selected)
    frame = _prepare_tushare_snapshot(*selected, preserve_invalid_rows=True)
    frame = frame.set_index("code").reindex(codes).reset_index()
    frame.attrs["snapshot_source"] = "tushare"
    frame.attrs["evidence_id"] = evidence_id
    manifest = DependencyManifest("tushare_snapshot_diagnostic", _NORMALIZATION_VERSION, CORE_FIELDS, ())
    scope = freeze_scope(manifest, codes, ())
    _, receipts, blockers = validate_snapshot(frame, CORE_FIELDS, codes, trade_date)
    result = assess_readiness(scope, receipts, trade_date=trade_date,
                              calendar_verified=calendar_verified, universe_verified=universe_verified,
                              optional_gaps=("input_fingerprint_not_source_authenticity_or_original_response_storage",))
    if blockers:
        result = replace(result, state="blocked", blockers=tuple(dict.fromkeys((*result.blockers, *blockers))))
    return result
