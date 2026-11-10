#!/usr/bin/env python3
"""Bounded, credential-free daily-data smoke probe (no DB, LLM, or notifications).

Run with the project's isolated interpreter. Only the existing Tencent adapter
is registered with DataFetcherManager; normal paid/credentialed routing is not
used. Output is JSON on stdout, with no project .env or user watchlist reads.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
CHINA_TIME = timezone(timedelta(hours=8))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def envelope(args: argparse.Namespace) -> dict:
    end = datetime.now(CHINA_TIME).date()
    start = end - timedelta(days=args.days * 2)
    return {
        "ok": False,
        "source": "TencentFetcher",
        "sourceUrl": SOURCE_URL,
        "command": "DataFetcherManager.get_daily_data",
        "symbol": args.symbol,
        "fetchedAt": utc_now(),
        "requestedWindow": {"start": str(start), "end": str(end)},
        "requestedAdjustment": "qfq",
        "reportedAdjustment": None,
        "count": 0,
        "records": [],
        "warnings": [],
    }


def worker(args: argparse.Namespace) -> dict:
    # Suppress provider exception strings: proxy URLs or environment-derived
    # diagnostics must not leak through this public-data probe.
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(PROJECT_ROOT))
    result = envelope(args)
    from data_provider.base import DataFetcherManager
    from data_provider.tencent_fetcher import TencentFetcher
    from src.services.run_diagnostics import (
        activate_run_diagnostic_context,
        get_current_diagnostic_context,
        reset_run_diagnostic_context,
    )

    class ObservedTencentFetcher(TencentFetcher):
        """Observe the adapter result without replacing its fetch/normalization."""

        raw_count = 0

        def _fetch_raw_data(self, stock_code, start_date, end_date):
            frame = super()._fetch_raw_data(stock_code, start_date, end_date)
            self.raw_count = len(frame)
            return frame

    fetcher = ObservedTencentFetcher()
    manager = DataFetcherManager(fetchers=[fetcher])
    token = activate_run_diagnostic_context(scope="market_data_probe")
    started = time.monotonic()
    try:
        frame, source = manager.get_daily_data(
            args.symbol,
            start_date=result["requestedWindow"]["start"],
            end_date=result["requestedWindow"]["end"],
            days=args.days,
        )
        result["source"] = source
        result["rawAdapterCount"] = fetcher.raw_count
        result["count"] = len(frame)
        result["columns"] = list(frame.columns)
        result["missingValues"] = {
            column: int(frame[column].isna().sum()) for column in frame.columns
        }
        result["units"] = {
            "open/high/low/close": "CNY per share or ETF unit (CN adapter contract)",
            "volume": "shares/ETF units; adapter converts non-STAR lots x100",
            "amount": "not verified; do not assume unit if absent",
            "pct_chg": "percentage points; calculated from adjacent returned closes",
            "ma5/ma10/ma20": "CNY; calculated by BaseFetcher",
            "volume_ratio": "dimensionless daily-volume ratio, not intraday ratio",
        }
        required = {"date", "open", "high", "low", "close", "volume"}
        missing = sorted(required.difference(frame.columns))
        result["missingRequiredFields"] = missing
        if frame.empty or missing:
            result["warnings"].append("empty_or_missing_required_fields")
            return result
        result["firstBarDate"] = frame["date"].min().strftime("%Y-%m-%d")
        result["latestBarDate"] = frame["date"].max().strftime("%Y-%m-%d")
        result["checks"] = {
            "duplicateDates": int(frame["date"].duplicated().sum()),
            "datesAscending": bool(frame["date"].is_monotonic_increasing),
            "nonpositivePrices": int(
                (frame[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()
            ),
            "negativeVolumes": int((frame["volume"] < 0).sum()),
            "invalidOHLC": int(
                (
                    (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
                    | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
                ).sum()
            ),
        }
        result["records"] = json.loads(
            frame.tail(3).assign(date=frame.tail(3)["date"].dt.strftime("%Y-%m-%d"))
            .to_json(orient="records", double_precision=10)
        )
        result["warnings"].extend([
            "daily_bars_not_a_realtime_feed",
            "qfq_requested_but_adapter_can_fallback_to_day_without_exposing_adjustment",
            "rawAdapterCount_is_after_provider_window_filter_not_original_HTTP_row_count",
            "first_pct_chg_is_zero_filled_without_prior_bar",
            "moving_average_warmup_uses_min_periods_1",
            "exchange_calendar_session_completeness_not_verified",
        ])
        if result["missingValues"].get("amount", 0):
            result["warnings"].append("amount_has_missing_values")
        if result["latestBarDate"] == result["requestedWindow"]["end"]:
            result["warnings"].append("latest_bar_is_today_and_may_be_incomplete")
        checks = result["checks"]
        result["ok"] = (
            not any(result["missingValues"].get(column, 0) for column in required)
            and checks["datesAscending"]
            and not any(
                checks[key]
                for key in ("duplicateDates", "nonpositivePrices", "negativeVolumes", "invalidOHLC")
            )
        )
    except Exception as exc:
        result["errorType"] = type(exc).__name__
        result["warnings"].append("free_source_fetch_failed_no_mock_or_paid_fallback")
    finally:
        context = get_current_diagnostic_context()
        if context:
            result["attempts"] = [
                {
                    key: getattr(run, key)
                    for key in ("provider", "success", "latency_ms", "error_type", "record_count")
                }
                for run in context.provider_runs
            ]
        reset_run_diagnostic_context(token)
        result["durationSeconds"] = round(time.monotonic() - started, 3)
        result["fetchedAt"] = utc_now()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="510300", help="Six-digit CN stock/ETF code; default: 510300")
    parser.add_argument(
        "--days", type=int, default=30,
        help="Lookback hint, 1..60; date window is twice this many calendar days",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Whole worker timeout in seconds, 1..60")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"\d{6}", args.symbol)
        or not 1 <= args.days <= 60
        or not 1 <= args.timeout <= 60
    ):
        parser.error("Use a six-digit CN symbol and days/timeout in 1..60.")
    if args.worker:
        try:
            result = worker(args)
        except Exception as exc:
            result = envelope(args)
            result["errorType"] = type(exc).__name__
            result["warnings"] = ["worker_import_or_initialization_failed"]
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result["ok"] else 1
    # Keep OS/network settings only, not application keys, notifications, LLM
    # configuration, or user watchlists. ENV_FILE=devnull prevents .env loading.
    allowed = {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP",
        "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE",
    }
    child_env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    child_env.update(
        ENV_FILE=os.devnull,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        STOCK_INDEX_REMOTE_UPDATE_ENABLED="false",
    )
    command = [
        sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
        "--symbol", args.symbol, "--days", str(args.days), "--timeout", str(args.timeout),
    ]
    try:
        completed = subprocess.run(
            command, cwd=PROJECT_ROOT, env=child_env, capture_output=True,
            text=True, encoding="utf-8", timeout=args.timeout, check=False,
        )
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        result = envelope(args)
        result.update(errorType="TimeoutExpired", warnings=["worker_exceeded_wall_clock_timeout"])
    except (OSError, ValueError, IndexError):
        result = envelope(args)
        result.update(errorType="ProbeWorkerError", warnings=["worker_did_not_return_valid_JSON"])
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
