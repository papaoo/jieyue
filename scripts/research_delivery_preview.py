"""LOCAL ONLY frozen research preview; never fetch, send, or authorize publication.

Version 1 bundle has exactly schema_version, trade_date (YYYY-MM-DD),
calendar_verified, universe_verified, universe, snapshot, daily_histories.
Both snapshot and each daily_histories[code] use {columns, index, data, attrs}:
unique string columns, scalar index, rectangular scalar rows, metadata object.
Snapshot attrs: snapshot_source, evidence_id, units_profile, pe_ratio_basis,
field_trade_date_columns (field -> column), optional max_trade_date.
History attrs: code, daily_source, evidence_id, price_adjustment, volume_unit,
optional stale/is_stale/daily_stale/history_stale/stale_fallback booleans.
Missing evidence is assessed as blocked, never inferred. Verification flags are
caller assertions, not independent exchange/calendar/collector verification.
No env configuration, URLs/credentials/config directives, live fallback or files
for enrichment are accepted. Only the supplied JSON file is read, bounded to
20 MiB. Results go to stdout, never to a cache or output file.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
import stat
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from src.services.research_delivery.evidence import canonical_code
from src.services.research_delivery.screening_preview import preview_strategy
from src.services.screening.config import Config
from src.services.screening.strategy import list_strategies


MAX_BUNDLE_BYTES = 20 * 1024 * 1024
BUNDLE_KEYS = {"schema_version", "trade_date", "calendar_verified", "universe_verified",
               "universe", "snapshot", "daily_histories"}
SNAPSHOT_ATTRS = {"snapshot_source", "evidence_id", "units_profile", "pe_ratio_basis",
                  "field_trade_date_columns", "max_trade_date"}
STALE_ATTRS = {"stale", "is_stale", "daily_stale", "history_stale", "stale_fallback"}
HISTORY_ATTRS = {"code", "daily_source", "evidence_id", "price_adjustment", "volume_unit", *STALE_ATTRS}


class BundleError(ValueError):
    """Only fixed local error codes are exposed, never exception messages."""


def _reject_constant(value):
    raise ValueError("nonfinite JSON constant")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _scalar(value):
    return (value is None or type(value) in (str, bool, int)
            or type(value) is float and math.isfinite(value))


def _frame(value, *, history=False):
    if type(value) is not dict or set(value) != {"columns", "index", "data", "attrs"}:
        raise BundleError("bundle_invalid_schema")
    columns, index, rows, attrs = (value[key] for key in ("columns", "index", "data", "attrs"))
    if (type(columns) is not list or not columns
            or any(type(column) is not str or not column.strip() for column in columns)
            or len(set(columns)) != len(columns)
            or type(index) is not list or type(rows) is not list or len(index) != len(rows)
            or any(not _scalar(item) for item in index)
            or any(type(row) is not list or len(row) != len(columns)
                   or any(not _scalar(cell) for cell in row) for row in rows)
            or type(attrs) is not dict or not set(attrs) <= (HISTORY_ATTRS if history else SNAPSHOT_ATTRS)):
        raise BundleError("bundle_invalid_schema")
    for key, item in attrs.items():
        if key == "field_trade_date_columns":
            if (type(item) is not dict
                    or any(type(field) is not str or not field.strip()
                           or type(column) is not str or not column.strip() for field, column in item.items())):
                raise BundleError("bundle_invalid_schema")
        elif key in STALE_ATTRS:
            if type(item) is not bool:
                raise BundleError("bundle_invalid_schema")
        elif type(item) is not str:
            raise BundleError("bundle_invalid_schema")
    # Object dtype preserves real booleans rather than silently coercing them to
    # 1/0 when another numeric row uses the same column; the adapter rejects them.
    frame = pd.DataFrame(rows, columns=columns, index=index, dtype=object)
    frame.attrs = attrs
    return frame


def load_bundle(path: Path):
    """Read bounded UTF-8 JSON and validate structure before invoking the adapter."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise BundleError("bundle_unreadable")
        if info.st_size > MAX_BUNDLE_BYTES:
            raise BundleError("bundle_too_large")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
    except OSError:
        raise BundleError("bundle_unreadable") from None
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleError("bundle_too_large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise BundleError("bundle_invalid_json") from None
    if (type(value) is not dict or set(value) != BUNDLE_KEYS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["trade_date"]) is not str
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value["trade_date"]) is None
            or type(value["calendar_verified"]) is not bool
            or type(value["universe_verified"]) is not bool):
        raise BundleError("bundle_invalid_schema")
    try:
        day = date.fromisoformat(value["trade_date"])
    except ValueError:
        raise BundleError("bundle_invalid_schema") from None
    universe, histories = value["universe"], value["daily_histories"]
    if (type(universe) is not list or not universe or any(not canonical_code(code) for code in universe)
            or len(set(universe)) != len(universe) or type(histories) is not dict
            or any(not canonical_code(code) or code not in universe for code in histories)):
        raise BundleError("bundle_invalid_schema")
    return dict(trade_date=day, universe=universe, calendar_verified=value["calendar_verified"],
                universe_verified=value["universe_verified"], snapshot=_frame(value["snapshot"]),
                daily_histories={code: _frame(frame, history=True) for code, frame in histories.items()})


def json_value(value):
    """Normalize only known JSON/date/numpy scalar types; never stringify objects."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        converted = float(value) if isinstance(value, np.floating) else value.item()
        if isinstance(converted, np.generic):
            raise TypeError("unsupported numpy output type")
        return json_value(converted)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite output")
        return value
    if type(value) in (list, tuple):
        return [json_value(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("non-string JSON key")
        return {key: json_value(item) for key, item in value.items()}
    raise TypeError("unsupported output type")


def _positive_int(raw):
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _parser():
    parser = argparse.ArgumentParser(description="LOCAL ONLY research preview; never authorizes publication.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="List actual built-in strategies (no env configuration).")
    preview = commands.add_parser("preview", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                  help="Assess a LOCAL frozen UTF-8 JSON bundle.")
    preview.add_argument("--bundle", type=Path, required=True, help="Version 1 JSON file, at most 20 MiB.")
    preview.add_argument("--strategy", required=True, help="Built-in strategy name from catalog.")
    preview.add_argument("--technical-limit", type=_positive_int, default=100, help="Positive candidate cap (default: 100).")
    return parser


def _error(code):
    return {"error": code, "preview_only": True, "publish_allowed": False}


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        runtime = Config()  # Explicit defaults, never Config.from_env().
        if args.command == "catalog":
            output = [dict(name=info.name, display_name=info.display_name, version=info.version,
                           requires_daily_features=info.requires_daily_features,
                           required_daily_fields=info.required_daily_fields)
                      for info in list_strategies(runtime.strategies_dir)]
            status = 0
        else:
            inputs = load_bundle(args.bundle)
            try:
                preview = preview_strategy(args.strategy, **inputs, technical_limit=args.technical_limit, config=runtime)
            except (TypeError, ValueError):
                raise BundleError("preview_invalid_request") from None
            scope = None if preview.scope is None else {
                key: {"count": len(getattr(preview.scope, key)), "codes": list(getattr(preview.scope, key))}
                for key in ("universe", "snapshot_pass", "technical_planned", "technical_not_evaluated")
            }
            output = dict(readiness=asdict(preview.readiness), scope=scope,
                          result=asdict(preview.result) if preview.result is not None else None,
                          trade_date=inputs["trade_date"], preview_only=True, publish_allowed=False)
            status = 2 if preview.readiness.state == "blocked" else 0
        # Build the whole strict document before printing: failures cannot leak a
        # partial result or arbitrary exception text.
        serialized = json.dumps(json_value(output), ensure_ascii=False, allow_nan=False)
    except BundleError as error:
        serialized, status = json.dumps(_error(error.args[0])), 2
    except Exception:
        serialized, status = json.dumps(_error("preview_failed")), 2
    print(serialized)
    return status


if __name__ == "__main__":
    # Windows stdout defaults may not encode Chinese names from another cwd.
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
