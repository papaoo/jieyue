# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Market snapshot fetcher.

Fetches full-market real-time snapshots for screening.
This is separate from single-stock realtime quotes.
"""

import logging
import json
import os
import random
import threading
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.services.screening.source_guard import call_with_timeout, parse_source_timeout_seconds

logger = logging.getLogger(__name__)

_SNAPSHOT_CACHE_VERSION = 1
_EM_REQUEST_MIN_INTERVAL_SECONDS = 1.0
_EM_REQUEST_JITTER_SECONDS = 0.3
_SOURCE_HEALTH_FAILURE_THRESHOLD = 3
_SOURCE_HEALTH_COOLDOWN_SECONDS = 5 * 60
_SNAPSHOT_CALL_TIMEOUT_SECONDS = 60.0
_EM_SESSION: requests.Session | None = None
_EM_LAST_REQUEST_AT = 0.0
_EM_LOCK = threading.Lock()
_SOURCE_HEALTH: dict[str, dict[str, object]] = {}
_SOURCE_HEALTH_LOCK = threading.Lock()
_INDUSTRY_CACHE: dict[str, object] = {}
_INDUSTRY_LOCK = threading.Lock()


def _supplement_snapshot_industry(frame: pd.DataFrame) -> None:
    """Fill only absent industry labels, with one bounded daily reference request."""
    if "code" not in frame:
        return
    values = frame.get("industry", pd.Series("", index=frame.index)).fillna("").astype(str).str.strip()
    missing = values.isin(["", "nan", "None", "unknown"])
    token = os.getenv("TUSHARE_TOKEN", "").strip() or os.getenv("TUSHARE_API_TOKEN", "").strip()
    if not missing.any() or not token:
        return
    with _INDUSTRY_LOCK:
        now = time.monotonic()
        if now >= float(_INDUSTRY_CACHE.get("expires", 0)):
            try:
                from data_provider.tushare_fetcher import create_tushare_http_client
                table = create_tushare_http_client(token, timeout=3).stock_basic(
                    exchange="", list_status="L", fields="ts_code,industry")
                if table is None or table.empty or not {"ts_code", "industry"}.issubset(table.columns):
                    raise ValueError("industry reference unavailable")
                table = table.dropna(subset=["ts_code", "industry"])
                mapping = dict(zip(table.ts_code.astype(str).str.split(".").str[0], table.industry.astype(str)))
                _INDUSTRY_CACHE.update(mapping=mapping, expires=now+86400,
                                       fetched_at=datetime.now(timezone.utc).isoformat())
            except Exception as exc:
                _INDUSTRY_CACHE.update(mapping={}, expires=now+60)
                frame.attrs.setdefault("data_warnings", []).append(f"industry reference unavailable: {type(exc).__name__}")
        mapping = dict(_INDUSTRY_CACHE.get("mapping", {}))
        acquired = _INDUSTRY_CACHE.get("fetched_at")
    labels = frame["code"].astype(str).str.zfill(6).map(mapping)
    usable = missing & labels.notna() & labels.ne("")
    if usable.any():
        frame["industry"] = values
        frame.loc[usable, "industry"] = labels.loc[usable]
        frame["industry_source"] = frame.get("industry_source", pd.Series("", index=frame.index))
        frame.loc[usable, "industry_source"] = "tushare:stock_basic"
        frame.attrs["industry_reference_fetched_at"] = acquired


def _annotate_snapshot_data_dates(frame: pd.DataFrame) -> None:
    """Acquisition time is not proof that every provider field is from today."""
    dates = {}
    warnings = list(frame.attrs.get("data_warnings", []))
    for column in ("MAX_TRADE_DATE", "trade_date", "daily_basic_trade_date"):
        if column not in frame:
            continue
        stamps = pd.to_datetime(frame[column].astype(str), errors="coerce").dropna()
        if stamps.empty:
            warnings.append(f"快照 {column} 无法解析，业务日期未知")
            continue
        dates[column] = str(stamps.max().date())
        warnings.append(f"快照 {column}={dates[column]}；抓取时间不等于业务日期，未核实字段不能视为当日实时数据")
    frame.attrs["snapshot_data_dates"] = dates
    frame.attrs["data_warnings"] = list(dict.fromkeys(warnings))


def fetch_cn_snapshot(source: str = "efinance") -> pd.DataFrame:
    """Fetch A-share full-market snapshot.

    Returns a DataFrame with columns:
        code, name, price, change_pct, amount, total_mv, circ_mv,
        pe_ratio, pb_ratio, volume_ratio, turnover_rate

    Raises RuntimeError if the source is unavailable.
    """
    if source == "sina":
        return _call_snapshot_wrapper(_fetch_sina, source=source)
    elif source == "efinance":
        return _call_snapshot_wrapper(_fetch_efinance, source=source)
    elif source == "akshare_em":
        return _call_snapshot_wrapper(_fetch_akshare_em, source=source)
    elif source == "em_datacenter":
        return _call_snapshot_wrapper(_fetch_em_datacenter, source=source)
    elif source == "tushare":
        return _call_snapshot_wrapper(_fetch_tushare, source=source)
    else:
        raise ValueError(f"Unknown snapshot source: {source}")


def fetch_snapshot_with_fallback(
    sources: list[str],
    *,
    required_columns: list[str] | None = None,
    fallback_snapshot_path: str | Path | None = None,
    fallback_max_age_hours: float | None = None,
    cache_ttl_seconds: float = 0.0,
    market: str = "cn",
) -> pd.DataFrame:
    """Try live sources, optionally falling back to the last-good snapshot."""
    if market == "us":
        return _fetch_us_snapshot_with_fallback(required_columns)

    errors = []
    required = required_columns or []
    if cache_ttl_seconds > 0:
        cached = _read_last_good_snapshot(
            fallback_snapshot_path,
            required_columns=required,
            source_errors=[],
            max_age_hours=cache_ttl_seconds / 3600.0,
            fresh=True,
            requested_snapshot_sources=sources,
        )
        if cached is not None:
            _supplement_snapshot_industry(cached)
            _annotate_snapshot_data_dates(cached)
            return cached

    for source in sources:
        disabled_reason = _source_disabled_reason(source)
        if disabled_reason:
            errors.append(f"{source}: {disabled_reason}")
            continue
        try:
            df = fetch_cn_snapshot(source)
            if not df.empty:
                missing = _missing_required_columns(df, required)
                if missing:
                    error = f"missing required columns {','.join(missing)}"
                    errors.append(f"{source}: {error}")
                    _record_source_failure(source, error)
                    continue
                df.attrs.setdefault("snapshot_source", source)
                df.attrs["source_errors"] = list(errors)
                df.attrs["fallback_used"] = False
                df.attrs["stale"] = False
                df.attrs["stale_age_hours"] = None
                _supplement_snapshot_industry(df)
                _annotate_snapshot_data_dates(df)
                _write_last_good_snapshot(
                    fallback_snapshot_path,
                    df,
                    source_priority=sources,
                )
                _record_source_success(source, rows=len(df))
                logger.info("Snapshot fetched from %s: %d rows", source, len(df))
                return df
            errors.append(f"{source}: returned empty data")
            _record_source_failure(source, "returned empty data")
        except Exception as e:
            errors.append(f"{source}: {e}")
            _record_source_failure(source, e)
            logger.warning("Snapshot source %s failed: %s", source, e)

    cached = _read_last_good_snapshot(
        fallback_snapshot_path,
        required_columns=required,
        source_errors=errors,
        max_age_hours=fallback_max_age_hours,
    )
    if cached is not None:
        _supplement_snapshot_industry(cached)
        _annotate_snapshot_data_dates(cached)
        return cached

    raise RuntimeError(f"All snapshot sources failed: {'; '.join(errors)}")


def _fetch_us_snapshot_with_fallback(
    required_columns: list[str] | None = None,
) -> pd.DataFrame:
    """Fetch US equity snapshot via yfinance adapter."""
    from src.services.screening.snapshot_us import fetch_us_snapshot

    df = fetch_us_snapshot()
    missing = _missing_required_columns(df, required_columns or [])
    if missing:
        logger.warning("US snapshot missing columns: %s", ",".join(missing))
    return df


def _missing_required_columns(df: pd.DataFrame, required_columns: list[str]) -> list[str]:
    missing: list[str] = []
    for col in required_columns:
        if col not in df.columns:
            missing.append(col)
            continue
        if df[col].dropna().empty:
            missing.append(col)
    return missing


def _call_snapshot_wrapper(fetcher, *, source: str) -> pd.DataFrame:
    return call_with_timeout(
        fetcher,
        timeout_sec=_snapshot_call_timeout_seconds(),
        label=f"snapshot source {source}",
    )


def _snapshot_call_timeout_seconds() -> float | None:
    return parse_source_timeout_seconds(
        "SCREENING_SNAPSHOT_CALL_TIMEOUT_SEC",
        default=_SNAPSHOT_CALL_TIMEOUT_SECONDS,
    )


def _source_disabled_reason(source: str) -> str | None:
    now = time.monotonic()
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.get(source)
        if not state:
            return None
        disabled_until = float(state.get("disabled_until", 0.0))
        if disabled_until <= now:
            if disabled_until:
                state["disabled_until"] = 0.0
            return None
        return f"temporarily disabled for {disabled_until - now:.1f}s after repeated failures"


def _record_source_success(source: str, *, rows: int | None = None) -> None:
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.setdefault(source, {"failures": 0.0, "disabled_until": 0.0})
        successes = float(state.get("successes", 0.0)) + 1.0
        state["successes"] = successes
        state["failures"] = 0.0
        state["disabled_until"] = 0.0
        state["last_success_at"] = time.time()
        if rows is not None:
            state["last_rows"] = float(rows)
            previous_avg = float(state.get("avg_rows", rows))
            state["avg_rows"] = previous_avg + (float(rows) - previous_avg) / successes


def _record_source_failure(source: str, error: object | None = None) -> None:
    now = time.monotonic()
    with _SOURCE_HEALTH_LOCK:
        state = _SOURCE_HEALTH.setdefault(source, {"failures": 0.0, "disabled_until": 0.0})
        failures = float(state.get("failures", 0.0)) + 1.0
        state["failures"] = failures
        state["total_failures"] = float(state.get("total_failures", 0.0)) + 1.0
        state["last_failure_at"] = time.time()
        if error is not None:
            state["last_error"] = " ".join(str(error).split())
        if failures >= _SOURCE_HEALTH_FAILURE_THRESHOLD:
            state["disabled_until"] = now + _SOURCE_HEALTH_COOLDOWN_SECONDS


def snapshot_source_health_snapshot(
    sources: list[str] | tuple[str, ...] | None = None,
) -> dict[str, dict[str, float | bool | str]]:
    """Return in-process snapshot-source health without exposing credentials."""
    now = time.monotonic()
    requested = tuple(sources or tuple(_SOURCE_HEALTH))
    with _SOURCE_HEALTH_LOCK:
        snapshot: dict[str, dict[str, float | bool | str]] = {}
        for source in requested:
            state = dict(_SOURCE_HEALTH.get(source, {}))
            disabled_until = float(state.get("disabled_until", 0.0))
            cooldown_remaining = max(disabled_until - now, 0.0)
            snapshot[source] = {
                "successes": float(state.get("successes", 0.0)),
                "failures": float(state.get("failures", 0.0)),
                "total_failures": float(state.get("total_failures", 0.0)),
                "last_rows": float(state.get("last_rows", 0.0)),
                "avg_rows": float(state.get("avg_rows", 0.0)),
                "disabled": disabled_until > now,
                "cooldown_remaining_seconds": round(cooldown_remaining, 4),
                "last_success_at": float(state.get("last_success_at", 0.0)),
                "last_failure_at": float(state.get("last_failure_at", 0.0)),
                "last_error": str(state.get("last_error", "")),
            }
    return snapshot


def _write_last_good_snapshot(
    path_like: str | Path | None,
    df: pd.DataFrame,
    *,
    source_priority: list[str] | None = None,
) -> None:
    if path_like is None:
        return
    path = Path(path_like)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _SNAPSHOT_CACHE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "snapshot_source": str(df.attrs.get("snapshot_source", "")),
                "data_warnings": list(df.attrs.get("data_warnings", [])),
                "snapshot_data_dates": df.attrs.get("snapshot_data_dates", {}),
                "industry_reference_fetched_at": df.attrs.get("industry_reference_fetched_at"),
                "source_priority": [
                    str(source).strip()
                    for source in (source_priority or [])
                    if str(source).strip()
                ],
                "row_count": int(len(df)),
                "columns": list(df.columns),
                **{
                    key: df.attrs[key]
                    for key in ("units_profile", "pe_ratio_basis", "field_trade_date_columns")
                    if key in df.attrs
                },
            },
            "frame": json.loads(
                df.to_json(orient="split", date_format="iso", force_ascii=False)
            ),
        }
        tmp_path = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
    except Exception as exc:  # noqa: BLE001 - live snapshot should remain usable.
        logger.warning("Failed to write last-good snapshot cache %s: %s", path, exc)


def _read_last_good_snapshot(
    path_like: str | Path | None,
    *,
    required_columns: list[str],
    source_errors: list[str],
    max_age_hours: float | None = None,
    fresh: bool = False,
    requested_snapshot_sources: list[str] | None = None,
) -> pd.DataFrame | None:
    if path_like is None:
        return None
    path = Path(path_like)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != _SNAPSHOT_CACHE_VERSION:
            raise ValueError("unsupported cache version")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("missing cache metadata")
        cached_snapshot_source = str(metadata.get("snapshot_source", "")).strip()
        if fresh and requested_snapshot_sources is not None:
            requested_priority = [
                str(source).strip()
                for source in requested_snapshot_sources
                if str(source).strip()
            ]
            cached_priority_raw = metadata.get("source_priority")
            cached_priority = (
                [str(source).strip() for source in cached_priority_raw if str(source).strip()]
                if isinstance(cached_priority_raw, list)
                else []
            )
            if cached_priority:
                if cached_priority != requested_priority:
                    raise ValueError(
                        "cached snapshot priority "
                        f"{','.join(cached_priority)} does not match requested priority "
                        f"{','.join(requested_priority)}"
                    )
            elif not requested_priority or cached_snapshot_source != requested_priority[0]:
                # Legacy cache entries did not persist the configured chain.
                # Reuse them only when their actual provider is still the
                # current primary source; a new live fetch will upgrade the
                # metadata and allow same-chain fallback reuse afterwards.
                raise ValueError(
                    "legacy cached snapshot source "
                    f"{cached_snapshot_source or '<missing>'} does not match requested primary "
                    f"{requested_priority[0] if requested_priority else '<missing>'}"
                )
        frame = payload.get("frame")
        if not isinstance(frame, dict):
            raise ValueError("missing cached frame")
        columns = frame.get("columns")
        data = frame.get("data")
        if not isinstance(columns, list) or not isinstance(data, list):
            raise ValueError("malformed cached frame")
        stale_age_hours = _cache_stale_age_hours(
            stat.st_mtime,
            created_at=str(payload.get("created_at", "")),
        )
        if max_age_hours is not None and max_age_hours >= 0 and stale_age_hours > max_age_hours:
            raise ValueError(
                f"cache stale_age_hours={stale_age_hours:.4g} exceeds max_age_hours={max_age_hours:.4g}"
            )
        cached = pd.DataFrame(data, columns=columns)
        if cached.empty:
            raise ValueError("cached snapshot is empty")
        missing = _missing_required_columns(cached, required_columns)
        if missing:
            raise ValueError(f"missing required columns {','.join(missing)}")
    except Exception as exc:  # noqa: BLE001 - invalid cache should not mask live errors.
        source_errors.append(f"last_good_cache: {exc}")
        return None

    cached.attrs["snapshot_source"] = "last_good_cache"
    cached.attrs["fallback_used"] = not fresh
    cached.attrs["cache_used"] = True
    cached.attrs["stale"] = not fresh
    cached.attrs["stale_age_hours"] = stale_age_hours
    cached.attrs["source_errors"] = list(source_errors)
    cached.attrs["last_good_snapshot_source"] = str(
        metadata.get("snapshot_source", "")
    )
    if isinstance(metadata, dict):
        cached.attrs["last_good_created_at"] = str(payload.get("created_at", ""))
        for key in (
            "data_warnings", "snapshot_data_dates", "industry_reference_fetched_at",
            "units_profile", "pe_ratio_basis", "field_trade_date_columns",
        ):
            if key in metadata:
                cached.attrs[key] = metadata[key]
    if fresh:
        logger.info(
            "Using fresh snapshot cache %s: age_hours=%s rows=%d",
            path,
            stale_age_hours,
            len(cached),
        )
    else:
        logger.warning(
            "Using last-good snapshot cache %s after live source failures: %s",
            path,
            "; ".join(source_errors),
        )
    return cached


def _cache_stale_age_hours(mtime: float, *, created_at: str = "") -> float:
    modified = _parse_created_at(created_at) or datetime.fromtimestamp(mtime, tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - modified).total_seconds() / 3600.0
    return round(max(age_hours, 0.0), 4)


def _parse_created_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fetch_efinance() -> pd.DataFrame:
    """Fetch via efinance."""
    import efinance as ef

    df = ef.stock.get_realtime_quotes()
    if df is None or df.empty:
        raise RuntimeError("efinance returned empty data")
    return _normalize(df, source="efinance")


def _fetch_akshare_em() -> pd.DataFrame:
    """Fetch via akshare (eastmoney)."""
    import akshare as ak

    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        raise RuntimeError("akshare returned empty data")
    return _normalize(df, source="akshare_em")


def _fetch_sina() -> pd.DataFrame:
    """Fetch A-share full-market snapshot directly from Sina Finance.

    Sina's market-center endpoint is a lightweight direct HTTP source with PE,
    PB, turnover and market-cap fields. It gives the screening engine another non-wrapper,
    non-Eastmoney-first snapshot option before falling back to Eastmoney-heavy
    sources.
    """
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    page = 1
    # Sina caps this endpoint at 100 rows. Use the cap to reduce full-market
    # pagination round trips without changing the response contract.
    page_size = 100
    all_items = []
    while True:
        resp = requests.get(
            url,
            params={
                "page": page,
                "num": page_size,
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://vip.stock.finance.sina.com.cn/mkt/",
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()
        if not isinstance(items, list):
            raise RuntimeError("sina snapshot returned malformed data")
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size:
            break
        page += 1
    if not all_items:
        raise RuntimeError("sina returned empty data")
    df = pd.DataFrame(all_items)
    for col in ("mktcap", "nmc"):
        if col in df.columns:
            # Sina exposes market caps in ten-thousand yuan; normalize to yuan.
            df[col] = pd.to_numeric(df[col], errors="coerce") * 10000
    return _normalize(df, source="sina")


def _fetch_em_datacenter() -> pd.DataFrame:
    """Fetch via eastmoney datacenter xuangu API.

    This works even on weekends (returns last trading day data).
    """
    url = "https://data.eastmoney.com/dataapi/xuangu/list"
    all_items = []
    page = 1
    page_size = 500

    while True:
        params = {
            "st": "SECURITY_CODE",
            "sr": "1",
            "ps": str(page_size),
            "p": str(page),
            "sty": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,NEW_PRICE,"
                   "CHANGE_RATE,VOLUME_RATIO,DEAL_AMOUNT,TURNOVERRATE,"
                   "PE9,PBNEWMRQ,TOTAL_MARKET_CAP,CIRCULATION_MARKET_CAP",
            "filter": '(MARKET+in+("上交所主板","深交所主板","深交所创业板","上交所科创板","北交所"))',
            "source": "SELECT_SECURITIES",
            "client": "WEB",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/xuangu/",
        }

        resp = _eastmoney_get(url, params=params, headers=headers, timeout=30)
        data = resp.json()

        if not data.get("success"):
            raise RuntimeError(f"em_datacenter API error: {data.get('message', 'unknown')}")

        items = data["result"]["data"]
        all_items.extend(items)

        total_count = data["result"]["count"]
        if page * page_size >= total_count:
            break
        page += 1

    if not all_items:
        raise RuntimeError("em_datacenter returned no data")

    df = pd.DataFrame(all_items)
    return _normalize(df, source="em_datacenter")


def _eastmoney_get(url: str, **kwargs) -> requests.Response:
    """GET EastMoney endpoints through one throttled shared session.

    EastMoney endpoints are useful but more sensitive to bursty access than
    lightweight direct sources. Keeping all direct calls behind one retrying
    session, a process-wide interval, and a little jitter follows the same
    anti-ban pattern used by a-stock-data's ``em_get`` helper.
    """
    global _EM_LAST_REQUEST_AT, _EM_SESSION
    with _EM_LOCK:
        if _EM_SESSION is None:
            _EM_SESSION = _build_eastmoney_session()
        elapsed = time.monotonic() - _EM_LAST_REQUEST_AT
        interval = _eastmoney_request_interval_seconds()
        if elapsed < interval:
            time.sleep(interval - elapsed)
        response = _EM_SESSION.get(url, **kwargs)
        _EM_LAST_REQUEST_AT = time.monotonic()
    response.raise_for_status()
    return response


def _build_eastmoney_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _eastmoney_request_interval_seconds() -> float:
    min_interval = _float_env(
        "SCREENING_EASTMONEY_MIN_INTERVAL_SEC",
        _EM_REQUEST_MIN_INTERVAL_SECONDS,
    )
    jitter = _float_env(
        "SCREENING_EASTMONEY_JITTER_SEC",
        _EM_REQUEST_JITTER_SECONDS,
    )
    return max(min_interval, 0.0) + random.uniform(0.0, max(jitter, 0.0))


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else float(default)


def _fetch_tushare() -> pd.DataFrame:
    """Fetch latest available A-share snapshot via Tushare Pro.

    Tushare is not a real-time source here. It is used as a resilient fallback
    by joining the latest open trading day's daily quote and daily_basic data.
    """
    token = (
        os.getenv("TUSHARE_TOKEN", "").strip()
        or os.getenv("TUSHARE_API_TOKEN", "").strip()
    )
    if not token:
        raise RuntimeError("tushare requires TUSHARE_TOKEN")

    from data_provider.tushare_fetcher import create_tushare_http_client

    pro = create_tushare_http_client(token)
    trade_date = _resolve_tushare_trade_date(pro)
    daily = None
    for _ in range(3):
        daily = pro.daily(trade_date=trade_date, fields="ts_code,trade_date,close,pct_chg,amount")
        if daily is not None and not daily.empty:
            break
        if os.getenv("TUSHARE_TRADE_DATE", "").strip():
            break  # An explicit replay date must never silently move.
        trade_date = _resolve_tushare_trade_date(pro, before_date=trade_date)
    if daily is None or daily.empty:
        raise RuntimeError(f"tushare daily returned empty data for {trade_date}")
    daily_basic = pro.daily_basic(
        trade_date=trade_date,
        fields="ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv",
    )
    stock_basic = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,industry",
    )

    if daily is None or daily.empty:
        raise RuntimeError(f"tushare daily returned empty data for {trade_date}")
    if daily_basic is None or daily_basic.empty:
        raise RuntimeError(f"tushare daily_basic returned empty data for {trade_date}")

    return _prepare_tushare_snapshot(daily, daily_basic, stock_basic)


def _resolve_tushare_trade_date(pro, *, before_date: str | None = None) -> str:
    """Return the latest open trade date for Tushare requests."""
    explicit = os.getenv("TUSHARE_TRADE_DATE", "").strip()
    if explicit and before_date is None:
        return explicit

    end = datetime.strptime(before_date, "%Y%m%d").date() - timedelta(days=1) if before_date else date.today()
    start = end - timedelta(days=30)
    calendar = pro.trade_cal(
        exchange="",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        is_open="1",
        fields="cal_date,is_open",
    )
    if calendar is None or calendar.empty or "cal_date" not in calendar.columns:
        raise RuntimeError("tushare trade_cal returned no open trading days")
    return str(calendar["cal_date"].max())


def _prepare_tushare_snapshot(
    daily: pd.DataFrame,
    daily_basic: pd.DataFrame,
    stock_basic: pd.DataFrame | None,
    *,
    preserve_invalid_rows: bool = False,
) -> pd.DataFrame:
    """Join and unit-normalize Tushare tables into the common snapshot schema."""
    for label, table in (("daily", daily), ("daily_basic", daily_basic), ("stock_basic", stock_basic)):
        if table is None:
            continue
        if not table.columns.is_unique:
            raise ValueError(f"tushare {label} has duplicate columns")
        if table.empty and label == "stock_basic":
            continue
        if "ts_code" not in table or table["ts_code"].isna().any() or not table["ts_code"].is_unique:
            raise ValueError(f"tushare {label} requires non-null unique ts_code")
        if "symbol" in table:
            canonical = table["ts_code"].astype(str).str.split(".").str[0]
            conflicts = table["symbol"].notna() & table["symbol"].astype(str).ne(canonical)
            if conflicts.any():
                raise ValueError(f"tushare {label} symbol conflicts with ts_code")

    # Each endpoint owns only these fields. Missing fields remain missing evidence.
    quotes = daily.reindex(columns=["ts_code", "trade_date", "close", "pct_chg", "amount"]).copy()
    basics = daily_basic.reindex(columns=[
        "ts_code", "trade_date", "turnover_rate", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv", "dv_ttm",
    ]).copy().rename(columns={"trade_date": "daily_basic_trade_date"})
    join = "outer" if preserve_invalid_rows else "left"
    merged = quotes.merge(basics, on="ts_code", how=join, validate="one_to_one")
    if stock_basic is not None and not stock_basic.empty:
        reference = stock_basic.reindex(columns=["ts_code", "name", "industry"]).copy()
        merged = merged.merge(reference, on="ts_code", how=join, validate="one_to_one")
    if preserve_invalid_rows:
        identities = pd.concat([table["ts_code"] for table in (daily, daily_basic, stock_basic)
                                if table is not None and "ts_code" in table]).drop_duplicates()
        merged = merged.set_index("ts_code").reindex(identities).reset_index()
    merged["code"] = merged["ts_code"].astype(str).str.split(".").str[0]
    normalized = _normalize(merged, source="tushare", preserve_invalid_rows=preserve_invalid_rows)

    # Tushare units: amount is thousand yuan; market caps are ten-thousand yuan.
    for col, multiplier in {
        "amount": 1000,
        "total_mv": 10000,
        "circ_mv": 10000,
    }.items():
        normalized[col] = normalized[col] * multiplier

    normalized.attrs["units_profile"] = "cn_equity_cny_v1"
    normalized.attrs["pe_ratio_basis"] = "ttm"
    normalized['dividend_yield_ttm_pct'] = pd.to_numeric(normalized['dv_ttm'].mask(
        normalized['dv_ttm'].map(lambda value: pd.api.types.is_bool(value) or isinstance(value, date))), errors='coerce')
    normalized.attrs['dividend_yield_ttm_unit'] = 'percent'
    normalized.attrs["field_trade_date_columns"] = {
        "price": "trade_date", "change_pct": "trade_date", "amount": "trade_date",
        "pe_ratio": "daily_basic_trade_date", "pb_ratio": "daily_basic_trade_date",
        "total_mv": "daily_basic_trade_date", "circ_mv": "daily_basic_trade_date",
        "turnover_rate": "daily_basic_trade_date", "volume_ratio": "daily_basic_trade_date",
        "dividend_yield_ttm_pct": "daily_basic_trade_date",
    }
    return normalized


def _normalize(df: pd.DataFrame, source: str, *, preserve_invalid_rows: bool = False) -> pd.DataFrame:
    """Normalize column names to a standard schema.

    Standard columns: code, name, price, change_pct, amount, total_mv,
                      circ_mv, pe_ratio, pb_ratio, volume_ratio, turnover_rate
    """
    df = df.copy()

    if source == "efinance":
        standard_cols = {
            "code": ["股票代码", "代码"],
            "name": ["股票名称", "名称"],
            "price": ["最新价"],
            "change_pct": ["涨跌幅"],
            "amount": ["成交额"],
            "total_mv": ["总市值"],
            "circ_mv": ["流通市值"],
            "pe_ratio": ["动态市盈率", "市盈率(动)"],
            "pb_ratio": ["市净率"],
            "volume_ratio": ["量比"],
            "turnover_rate": ["换手率"],
            "industry": ["行业", "所属行业", "行业板块"],
            "concepts": ["概念", "概念题材", "题材"],
        }
    elif source == "akshare_em":
        standard_cols = {
            "code": ["代码"],
            "name": ["名称"],
            "price": ["最新价"],
            "change_pct": ["涨跌幅"],
            "amount": ["成交额"],
            "total_mv": ["总市值"],
            "circ_mv": ["流通市值"],
            "pe_ratio": ["市盈率-动态", "市盈率(动)"],
            "pb_ratio": ["市净率"],
            "volume_ratio": ["量比"],
            "turnover_rate": ["换手率"],
            "industry": ["行业", "所属行业", "行业板块"],
            "concepts": ["概念", "概念题材", "题材"],
        }
    elif source == "sina":
        standard_cols = {
            "code": ["code"],
            "name": ["name"],
            "price": ["trade"],
            "change_pct": ["changepercent"],
            "amount": ["amount"],
            "total_mv": ["mktcap"],
            "circ_mv": ["nmc"],
            "pe_ratio": ["per"],
            "pb_ratio": ["pb"],
            "turnover_rate": ["turnoverratio"],
        }
    elif source == "em_datacenter":
        standard_cols = {
            "code": ["SECURITY_CODE"],
            "name": ["SECURITY_NAME_ABBR"],
            "price": ["NEW_PRICE"],
            "change_pct": ["CHANGE_RATE"],
            "amount": ["DEAL_AMOUNT"],
            "total_mv": ["TOTAL_MARKET_CAP"],
            "circ_mv": ["CIRCULATION_MARKET_CAP"],
            "pe_ratio": ["PE9"],
            "pb_ratio": ["PBNEWMRQ"],
            "volume_ratio": ["VOLUME_RATIO"],
            "turnover_rate": ["TURNOVERRATE"],
            "industry": ["INDUSTRY", "INDUSTRY_NAME", "BOARD_NAME"],
            "concepts": ["CONCEPT", "CONCEPT_NAME", "THEME_NAME"],
        }
    elif source == "tushare":
        standard_cols = {
            "code": ["code", "symbol"],
            "name": ["name"],
            "price": ["close"],
            "change_pct": ["pct_chg"],
            "amount": ["amount"],
            "total_mv": ["total_mv"],
            "circ_mv": ["circ_mv"],
            "pe_ratio": ["pe_ttm"],
            "pb_ratio": ["pb"],
            "volume_ratio": ["volume_ratio"],
            "turnover_rate": ["turnover_rate"],
            "industry": ["industry"],
            "concepts": ["concepts"],
        }
    else:
        standard_cols = {}

    df = _rename_standard_columns(df, standard_cols)

    # Coerce numeric columns
    numeric_cols = [
        "price", "change_pct", "amount", "total_mv", "circ_mv",
        "pe_ratio", "pb_ratio", "volume_ratio", "turnover_rate",
    ]
    for col in numeric_cols:
        if col in df.columns:
            if source == "tushare":
                # Datetime dtypes otherwise become finite nanosecond integers.
                invalid = df[col].map(lambda value: pd.api.types.is_bool(value) or isinstance(value, date))
                df[col] = df[col].astype(object).mask(invalid)
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop rows without a valid price
    if "price" in df.columns and not preserve_invalid_rows:
        df = df.dropna(subset=["price"])
        df = df[df["price"] > 0]

    df.attrs["snapshot_source"] = source
    return df


def _rename_standard_columns(
    df: pd.DataFrame,
    standard_cols: dict[str, list[str]],
) -> pd.DataFrame:
    """Rename the first matching source column for each standard field."""
    rename_map: dict[str, str] = {}
    for standard_name, candidates in standard_cols.items():
        for candidate in candidates:
            if candidate in df.columns:
                rename_map[candidate] = standard_name
                break
    return df.rename(columns=rename_map)
