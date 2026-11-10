"""Offline collector-assertion validation; labels are not cryptographic proof.

The collector remains responsible for original responses, unit conversions,
adjustment factors and verified contiguous exchange sessions. This validator
checks supplied cells, not those external claims' authenticity.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from functools import lru_cache
import math
import re
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.services.research_delivery.contracts import CellKey, FieldReceipt
from src.services.screening.pipeline import _copy_frozen_frame
from src.services.screening.industry_reference import INDUSTRY_FIELDS, industry_identity


CORE_FIELDS = ("name", "price", "amount", "total_mv", "pe_ratio", "pb_ratio",
               "turnover_rate", "volume_ratio", "change_pct")


def canonical_code(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]{6}", value) is not None


def business_date(value: object) -> date | None:
    if isinstance(value, str):
        fmt = "%Y-%m-%d" if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) else "%Y%m%d"
        if fmt == "%Y%m%d" and not re.fullmatch(r"[0-9]{8}", value):
            return None
        try:
            return _parse_business_date_string(value, fmt)
        except ValueError:
            return None
    if isinstance(value, datetime):
        if pd.isna(value) or value.time() != datetime.min.time() or value.tzinfo is not None:
            return None
        return value.date()
    return value if isinstance(value, date) else None


@lru_cache(maxsize=4096)
def _parse_business_date_string(value, fmt):
    return datetime.strptime(value, fmt).date()


def finite_number(value: object) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("boolean is not a numeric observation")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("nonfinite numeric observation")
    return converted


def nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_snapshot(
    snapshot: pd.DataFrame, fields: tuple[str, ...], universe: tuple[str, ...], trade_date: date,
) -> tuple[pd.DataFrame | None, list[FieldReceipt], list[str]]:
    frame = _copy_frozen_frame(snapshot)
    if frame.columns.has_duplicates or "code" not in frame.columns:
        return None, [], ["snapshot_identity_invalid"]
    codes = frame["code"].tolist()
    if (any(not canonical_code(code) for code in codes) or len(set(codes)) != len(codes)
            or set(codes) != set(universe)):
        return None, [], ["snapshot_universe_mismatch"]
    blockers = []
    if frame.attrs.get("units_profile") != "cn_equity_cny_v1":
        blockers.append("snapshot_units_profile_invalid")
    if frame.attrs.get("pe_ratio_basis") != "ttm":
        blockers.append("snapshot_pe_ratio_basis_invalid")
    source, evidence_id = frame.attrs.get("snapshot_source"), frame.attrs.get("evidence_id")
    source = source if nonblank(source) else ""
    evidence_id = evidence_id if nonblank(evidence_id) else ""
    dates = frame.attrs.get("field_trade_date_columns", {})
    if not isinstance(dates, Mapping):
        dates = {}
    receipts = []
    accepted = pd.DataFrame({"code": codes})
    for field in fields:
        values = []
        for index, code in enumerate(codes):
            status = "valid"
            value = frame[field].iloc[index] if field in frame.columns else None
            date_column = dates.get(field)
            as_of = business_date(frame[date_column].iloc[index]) if isinstance(date_column, str) and date_column in frame.columns else None
            cell_source, cell_evidence = source, evidence_id
            override_map = frame.attrs.get('field_source_overrides', {})
            code_overrides = override_map.get(code, {}) if isinstance(override_map, Mapping) else {}
            overrides = code_overrides.get(field) if isinstance(code_overrides, Mapping) else None
            if overrides is not None:
                if (not isinstance(overrides, Mapping) or field != 'pb_ratio' or overrides.get('source') != 'baostock.pbMRQ'
                        or business_date(overrides.get('effective_as_of')) != trade_date
                        or not nonblank(overrides.get('evidence_id'))):
                    status = 'invalid'
                else:
                    cell_source, cell_evidence = overrides['source'], overrides['evidence_id']
            if field == "name" and as_of is None:
                # Reference metadata is observed today, not a fabricated price date.
                metadata = frame.attrs.get("reference_field_metadata", {})
                reference = metadata.get("name", {}) if isinstance(metadata, Mapping) else {}
                if isinstance(reference, Mapping):
                    try:
                        observed = datetime.fromisoformat(reference.get("observed_at", ""))
                        effective = business_date(reference.get("effective_as_of"))
                        if (observed.utcoffset() is not None and effective == trade_date
                                and observed.astimezone(ZoneInfo("Asia/Shanghai")).date() == trade_date
                                and nonblank(reference.get("source")) and nonblank(reference.get("evidence_id"))):
                            as_of = effective
                            cell_source, cell_evidence = reference["source"], reference["evidence_id"]
                    except (ValueError, TypeError):
                        pass
            try:
                if field == "name":
                    if not nonblank(value):
                        raise ValueError("invalid name")
                else:
                    value = finite_number(value)
                    if (field == "price" and value <= 0) or (
                        field in {"amount", "total_mv", "turnover_rate", "volume_ratio"} and value < 0
                    ):
                        raise ValueError("invalid numeric domain")
            except (ValueError, TypeError, OverflowError):
                status = "invalid"
            values.append(value)
            receipts.append(FieldReceipt(CellKey("snapshot", code, field), status, as_of, cell_source, cell_evidence))
        accepted[field] = values
    # Preserve only complete dated canonical references, never provider-name guesses.
    for position, (_, row) in enumerate(frame.iterrows()):
        if industry_identity(row, trade_date):
            for field in INDUSTRY_FIELDS:
                if field not in accepted:
                    accepted[field] = ''
                accepted.loc[position, field] = row[field]
    dividend = 'dividend_yield_ttm_pct'
    if dividend in frame and dividend not in accepted:
        values = []
        for index in range(len(frame)):
            try:
                value = finite_number(frame[dividend].iloc[index])
                column = dates.get(dividend)
                if (frame.attrs.get('dividend_yield_ttm_unit') != 'percent' or value < 0
                        or column not in frame or business_date(frame[column].iloc[index]) != trade_date):
                    raise ValueError('unverified_dividend')
                values.append(value)
            except (ValueError, TypeError):
                values.append(None)
        accepted[dividend] = values
        accepted['dividend_yield_ttm_as_of'] = [trade_date.isoformat() if value is not None else '' for value in values]
        accepted['dividend_yield_ttm_unit'] = ['percent' if value is not None else '' for value in values]
    accepted.attrs = dict(frame.attrs, signal_date=trade_date.isoformat())
    return accepted, receipts, blockers


def validate_history(history: pd.DataFrame, code: str, trade_date: date, price: float) -> pd.DataFrame:
    if not isinstance(history, pd.DataFrame):
        raise ValueError("missing raw history")
    frame = _copy_frozen_frame(history)
    required = ("date", "open", "high", "low", "close", "volume")
    if frame.columns.has_duplicates or not set(required) <= set(frame.columns) or len(frame) < 61:
        raise ValueError("raw history schema or warmup invalid")
    if (frame.attrs.get("code") != code or frame.attrs.get("price_adjustment") != "forward"
            or frame.attrs.get("volume_unit") != "shares"
            or not nonblank(frame.attrs.get("daily_source")) or not nonblank(frame.attrs.get("evidence_id"))):
        raise ValueError("raw history identity/provenance invalid")
    if any(frame.attrs.get(key) for key in ("stale", "is_stale", "daily_stale", "history_stale", "stale_fallback")):
        raise ValueError("history marked stale")
    dates = [business_date(value) for value in frame["date"]]
    if (any(value is None or value > trade_date for value in dates)
            or any(left >= right for left, right in zip(dates, dates[1:])) or dates[-1] != trade_date):
        raise ValueError("raw history business dates invalid")
    accepted = frame.loc[:, list(required)].copy()
    accepted["date"] = pd.to_datetime(dates)
    for field in required[1:]:
        accepted[field] = [finite_number(value) for value in frame[field]]
        invalid_domain = (accepted[field] < 0).any() if field == "volume" else (accepted[field] <= 0).any()
        if invalid_domain:
            raise ValueError("raw history numeric domain invalid")
    if ((accepted["low"] > accepted[["open", "close"]].min(axis=1)).any()
            or (accepted["high"] < accepted[["open", "close"]].max(axis=1)).any()):
        raise ValueError("raw history OHLC relationships invalid")
    if abs(float(accepted["close"].iloc[-1]) - price) > max(.01, .001 * price):
        raise ValueError("end-bar price scale conflicts with snapshot")
    accepted.attrs = frame.attrs
    return accepted
