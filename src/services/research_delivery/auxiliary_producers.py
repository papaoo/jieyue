"""Bounded fact producers. No LLM, publication, notification or config loading."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd

from src.services.research_delivery.evidence import business_date as parse_date, finite_number
from src.services.research_delivery.evidence_store import EvidenceStore
from src.services.research_delivery.live_contracts import ProducerResult, json_safe
from src.services.research_delivery.market_context import empty_market_context, instrument, load_market_context


SHANGHAI = ZoneInfo("Asia/Shanghai")
CATEGORIES = {"etf_rotation", "market_radar", "announcement_risk"}


class _InvalidFacts(Exception):
    def __init__(self, code, *, coverage=None, warnings=()):
        super().__init__(code)
        self.coverage, self.warnings = coverage or {}, list(warnings)


class _Interrupted(Exception):
    def __init__(self, error):
        self.error = error


def _progress(callback, **payload):
    if callback:
        try:
            callback(payload)
        except Exception as exc:
            raise _Interrupted(exc) from None


def _current_stamp(value, target, now):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.utcoffset() is not None and stamp.astimezone(SHANGHAI).date() == target and stamp <= now
    except (TypeError, ValueError):
        return False


def _source(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) else None


def _manager():
    # The general manager's default constructor loads global config. This
    # request-local existing provider requires none and confirms qfq explicitly.
    from data_provider import DataFetcherManager
    from data_provider.tencent_fetcher import TencentFetcher
    return DataFetcherManager(fetchers=[TencentFetcher()])


def _etf(config, target, clock, store, evidence, manager, progress):
    from data_provider.base import _is_etf_code, safe_exception_details
    from src.core.etf_rotation import latest_ranking
    from src.services.etf_rotation_service import load_closes, build_report, params_from_config
    from src.services.stock_list_parser import _classify_bare_code

    pool = list(dict.fromkeys(getattr(config, "etf_rotation_pool", [])))
    safe = getattr(config, "etf_rotation_safe_asset", "") or None
    codes = list(dict.fromkeys(pool + ([safe] if safe else [])))
    if not pool or any(not isinstance(c, str) or not re.fullmatch(r"[15]\d{5}", c) or not _is_etf_code(c) for c in codes):
        raise _InvalidFacts("etf_pool_invalid")
    exchanges = {code: _classify_bare_code(code)[0] for code in codes}
    if any(exchange not in {"SH", "SZ"} for exchange in exchanges.values()):
        raise _InvalidFacts("etf_pool_invalid")
    params = params_from_config(config)
    _progress(progress, stage="collecting", category_id="etf_rotation")
    default_manager = manager is None
    manager = manager if manager is not None else _manager()
    loaded = load_closes(manager, codes, years=1, end=target, safe_errors=True)
    closes = loaded.closes
    if loaded.failed or closes.empty or any(c not in closes for c in codes):
        raise _InvalidFacts("etf_asset_data_unavailable")
    if (not isinstance(closes.index, pd.DatetimeIndex) or not closes.index.is_unique
            or closes.index[-1].date() != target or not closes.index.is_monotonic_increasing
            or closes.index[0].date() < target - timedelta(days=366)):
        raise _InvalidFacts("etf_history_date_invalid")
    sources = {c: _source(loaded.sources.get(c)) for c in codes}
    if not all(sources.values()):
        raise _InvalidFacts("etf_source_missing")
    for code in codes:
        series = closes[code]
        values = [finite_number(v) for v in series.dropna()]
        if not values or min(values) <= 0 or pd.isna(series.iloc[-1]):
            raise _InvalidFacts("etf_latest_quote_missing")
        # Warm up every configured asset, including defense, on observed quotes.
        if len(series) <= params.lookback_days or series.tail(params.lookback_days + 1).isna().any():
            raise _InvalidFacts("etf_warmup_incomplete")
        if series.dropna().pct_change(fill_method=None).abs().gt(.25).any():
            raise _InvalidFacts("etf_abnormal_price_jump")
    evidence.append(store.capture_payload("etf_providers", "etf_closes", dict(
        columns=codes, dates=[d.date().isoformat() for d in closes.index],
        rows=[[None if pd.isna(v) else float(v) for v in row] for row in closes[codes].itertuples(index=False, name=None)],
        sources=sources, price_adjustment="forward"), fetched_at=clock(), business_date=target,
        provenance="provider_normalized"))
    notes = ["one_year_history_signal_only", "rebalance_session_unverified_reference_only"]
    if default_manager:
        notes.append("etf_default_single_tencent_provider")
    if loaded.rejected:
        notes.append("etf_provider_attempt_rejected")
    if closes[codes].isna().any().any():
        notes.append("etf_historical_quotes_missing")
    if len(set(sources.values())) > 1:
        notes.append("etf_mixed_forward_adjusted_sources")
    context = empty_market_context(target)
    resolver = getattr(manager, "get_stock_name", None)
    resolver_source = type(manager).__name__ + ".get_stock_name" if callable(resolver) else "name_resolver_unavailable"
    if not _source(resolver_source):
        resolver_source = "existing_name_resolver"
    for index, code in enumerate(codes):
        try:
            # Existing manager semantics: False skips heavy realtime prefetch,
            # but still tries its cache/static references and provider methods.
            with safe_exception_details(enabled=True):
                name = resolver(code, allow_realtime=False) if callable(resolver) and index < 30 else None
        except Exception:
            name = None
        context["instruments"][code] = instrument(code, name, asset_type="etf",
            source=resolver_source if index < 30 else "name_lookup_limit_reached",
            observed_at=clock().isoformat(), as_of=target.isoformat(), market="cn", exchange=exchanges[code],
            ts_code=code + "." + exchanges[code], instrument_id=f"cn:{exchanges[code]}:etf:{code}",
            identity_source="stock_list_parser._classify_bare_code")
    if len(codes) > 30:
        notes.append("etf_name_lookup_limit_reached")
    if any(value["status"] == "unknown" for value in context["instruments"].values()):
        notes.append("etf_instrument_names_unknown")
    evidence.append(store.capture_payload("dsa", "instrument_names", dict(instruments=context["instruments"]),
        fetched_at=clock(), business_date=target, provenance="provider_normalized"))
    context["evidence"] = [dict(evidence[-1])]
    labels = {code: value["name"] or f"未知名称（{code}）" for code, value in context["instruments"].items()}
    report = build_report(closes, pool, safe, params, labels)
    if report.data_quality == "blocked" or not report.target_weights:
        raise _InvalidFacts("etf_report_data_quality_blocked")
    _, ranking = latest_ranking(closes, pool, params)
    if ranking.isna().any():
        raise _InvalidFacts("etf_momentum_unavailable")
    items = [dict(id=c, name=context["instruments"][c]["name"], metrics=dict(momentum=float(score), close=float(closes[c].iloc[-1])),
                  as_of=target.isoformat(), source=sources[c]) for c, score in ranking.items()]
    result = dict(items=items, markdown=report.markdown, ranking=[dict(code=i["id"], **i["metrics"]) for i in items],
        target_weights=report.target_weights, sources=sources, as_of=target.isoformat(), research_context=context,
        execution_assumptions=dict(history_years=1, price_adjustment="forward", mode="reference_only",
            rebalance=params.rebalance, lookback_days=params.lookback_days, top_n=params.top_n,
            cost_bps=params.cost_bps, execution="next_trading_session_close", defensive_asset=safe,
            performance_statistics="omitted"))
    return result, dict(asset_count=len(codes), valid_asset_count=len(codes), history_years=1), notes


def _trusted_batches(context, target):
    batches = context.get("stock_batches", []) if isinstance(context, dict) else []
    if not isinstance(batches, list):
        return []
    return [b for b in batches if isinstance(b, dict) and b.get("business_date") == target.isoformat()
            and b.get("state") in {"ready", "degraded"} and b.get("blockers") == []]


def _breadth(context, target, now, store):
    """Only locally captured same-day daily tables, not arbitrary context totals."""
    for batch in _trusted_batches(context, target):
        try:
            coverage = batch["coverage"]
            ratio = finite_number(coverage["daily_response_coverage"])
            universe = finite_number(coverage["independent_universe_count"])
            if not .95 <= ratio <= 1 or universe <= 0:
                continue
            for entry in batch.get("evidence", []):
                if (entry.get("source") != "tushare" or entry.get("endpoint") != "daily"
                        or entry.get("params") != {"trade_date": target.strftime("%Y%m%d")}):
                    continue
                path = Path(entry["path"]).resolve(strict=True)
                if not path.is_relative_to(store.directory.resolve()) or not path.is_file() or path.stat().st_size > 10_000_000:
                    continue
                raw = path.read_bytes()
                if sha256(raw).hexdigest() != entry.get("sha256"):
                    continue
                data = json.loads(raw)
                if (data.get("source") != "tushare" or data.get("endpoint") != "daily"
                        or data.get("params") != entry["params"] or data.get("business_date") != target.isoformat()
                        or not _current_stamp(data.get("fetched_at"), target, now)):
                    continue
                table = pd.DataFrame(data["rows"], columns=data["columns"])
                if (not table.columns.is_unique or not {"ts_code", "trade_date", "pct_chg", "amount"} <= set(table)
                        or len(table) == 0 or len(table) != coverage["daily_response_count"]
                        or abs(len(table) / universe - ratio) > 1e-9 or table.ts_code.duplicated().any()
                        or any(not isinstance(c, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", c) for c in table.ts_code)
                        or any(parse_date(d) != target for d in table.trade_date)):
                    continue
                changes = [finite_number(v) for v in table.pct_chg]
                amounts = [finite_number(v) for v in table.amount]
                if min(amounts) < 0:
                    continue
                metrics = dict(advancers=sum(v > 0 for v in changes), decliners=sum(v < 0 for v in changes),
                    flat=sum(v == 0 for v in changes), observed_count=len(changes), turnover_yuan=sum(amounts) * 1000.)
                verified = dict(source="tushare", endpoint="daily", sha256=entry["sha256"], evidence_id=entry["sha256"],
                    path=str(path), fetched_at=data["fetched_at"], business_date=target.isoformat(), params=data["params"],
                    row_count=len(table))
                return dict(id="market:breadth", name="市场宽度", metrics=metrics,
                    source="tushare.daily:observed_snapshot", as_of=target.isoformat()), verified, ratio
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return None, None, None


def _radar(target, clock, store, evidence, service, manager, context, progress):
    from src.services.market_hotspot_service import MarketHotspotService

    items, warnings = [], []
    shared = load_market_context(context.get("stock_batches", []) if isinstance(context, dict) else [],
        business_date=target, evidence_dir=store.directory, now=clock())
    evidence.extend(shared["evidence"])
    observed = set()
    for row in shared["industries"].get("all", [])[:10]:
        identity = "industry:" + sha256(("computed_from_tushare:" + row["name"]).encode()).hexdigest()[:16]
        items.append(dict(id=identity, name=row["name"], metrics={field: row[field]
            for field in ("change_pct", "rank", "population_count", "covered_count", "coverage_ratio",
                          "advancers", "decliners", "flat", "unknown_count")},
            as_of=target.isoformat(), source="computed_from_tushare"))
        observed.add("industry")
    breadth, reference, ratio = _breadth(context, target, clock(), store)
    if breadth:
        items.append(breadth)
        evidence.append(reference)
    _progress(progress, stage="collecting", category_id="market_radar")
    try:
        if service is None and manager is not None:
            service = MarketHotspotService(fetcher_manager=manager, ranking_fetch_timeout_seconds=3.)
        if service is None:
            payload = {}
            warnings.append("hotspot_date_verified_provider_not_requested")
        else:
            payload = service.get_hotspots(market="cn", limit=5)
    except Exception:
        payload = {}
        warnings.append("hotspot_source_failed")
    for group, family in (("leading_industries", "industry"), ("leading_concepts", "concept"), ("lagging_themes", None)):
        rows = payload.get(group, []) if isinstance(payload, dict) else []
        for row in rows[:10] if isinstance(rows, list) else []:
            try:
                kind = row.get("source")
                provider = _source(row.get("provider"))
                name = row.get("name")
                if kind == "industry" and shared["industries"]:
                    continue  # One industry definition per result: prefer verified shared facts.
                if (kind not in {"industry", "concept"} or (family and kind != family)
                        or parse_date(row.get("as_of")) != target or not provider
                        or not isinstance(name, str) or not name.strip() or len(name) > 120):
                    warnings.append("hotspot_stale_undated_or_invalid_rows_excluded")
                    continue
                metrics = dict(change_pct=finite_number(row.get("change_pct")))
                if row.get("rank") is not None:
                    rank = finite_number(row["rank"])
                    if rank < 1 or not rank.is_integer():
                        raise ValueError("invalid rank")
                    metrics["rank"] = int(rank)
                identity = kind + ":" + sha256((provider + ":" + name).encode()).hexdigest()[:16]
                if any(item["id"] == identity for item in items):
                    continue
                items.append(dict(id=identity, name=name, metrics=metrics, as_of=target.isoformat(), source=provider))
                observed.add(kind)
            except (TypeError, ValueError, AttributeError):
                warnings.append("hotspot_invalid_rows_excluded")
    for kind in ("industry", "concept"):
        if kind not in observed:
            warnings.append(kind + "_rankings_unavailable")
    coverage = dict(scope="observed_snapshot_not_full_market" if breadth else "verified_theme_subset",
                    industry_available="industry" in observed, concept_available="concept" in observed)
    if breadth:
        coverage["daily_response_coverage"] = ratio
    shared["instruments"] = {}
    shared["statuses"].update(industry="available" if "industry" in observed else "unavailable",
                              concept="available" if "concept" in observed else "unavailable")
    evidence.append(store.capture_payload("dsa", "market_radar", dict(items=items,
        source_metadata=dict(shared_context_rejections=shared["rejections"], shared_context_missing=shared["missing"],
            industry_source=shared["industries"].get("source"), hotspot_requested=service is not None,
            rejection_codes=sorted(set(warnings)))), fetched_at=clock(),
        business_date=target, provenance="derived"))
    if not items:
        raise _InvalidFacts("radar_current_verified_facts_unavailable", coverage=coverage,
                            warnings=sorted(set(warnings)))
    return dict(items=items, as_of=target.isoformat(), research_context=shared), coverage, warnings


def _ann_url(value, source, code):
    if not isinstance(value, str) or len(value) > 1000:
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            return False
        if source == "CNINFO":
            return parsed.hostname == "static.cninfo.com.cn" and bool(re.fullmatch(r"/finalpage/[\w/.-]+\.pdf", parsed.path, re.I))
        if source == "EastmoneyAnnouncement":
            return parsed.hostname == "data.eastmoney.com" and bool(re.fullmatch(r"/notices/detail/" + code + r"/[A-Za-z0-9]+\.html", parsed.path))
    except ValueError:
        pass
    return False


def _announcements(config, target, clock, store, evidence, fetcher, context, progress):
    from data_provider.base import normalize_stock_code
    from src.announcement_source import fetch_company_announcements

    configured = getattr(config, "stock_list", [])
    if not isinstance(configured, (list, tuple)):
        raise _InvalidFacts("announcement_scope_invalid")
    codes = []
    for value in configured:
        # Only A-share symbols; no private positions or portfolio fields read.
        code = normalize_stock_code(value) if isinstance(value, str) else ""
        if re.fullmatch(r"[03689]\d{5}", code):
            codes.append(code)
    for batch in _trusted_batches(context, target):
        picks = batch.get("result", {}).get("picks", [])
        if isinstance(picks, list):
            codes.extend(p["code"] for p in picks if isinstance(p, dict) and isinstance(p.get("code"), str)
                         and re.fullmatch(r"[03689]\d{5}", p["code"]))
    codes = list(dict.fromkeys(codes))
    if not codes:
        raise _InvalidFacts("announcement_scope_empty")
    shared = load_market_context(context.get("stock_batches", []) if isinstance(context, dict) else [],
        business_date=target, evidence_dir=store.directory, now=clock())
    shared["instruments"] = {code: shared["instruments"].get(code) or instrument(code, None,
        asset_type="stock", source="current_exact_reference_unavailable", observed_at=None,
        as_of=target.isoformat()) for code in codes[:30]}
    evidence.extend(shared["evidence"])
    scanned, failed, verified, excluded = 0, 0, 0, 0
    records = []
    for code in codes[:30]:
        clock()  # A long bounded scan must stop when the business date changes.
        _progress(progress, stage="collecting", category_id="announcement_risk", code=code)
        scanned += 1
        try:
            response = (fetcher or fetch_company_announcements)(code, days=3, limit=10, as_of=target)
            if (not isinstance(response, dict) or response.get("ok") is not True
                    or not _current_stamp(response.get("fetched_at"), target, clock())
                    or not isinstance(response.get("records"), list)):
                failed += 1
                continue
            verified += 1
            for row in response["records"][:10]:
                if not isinstance(row, dict):
                    excluded += 1
                    continue
                published = parse_date(row.get("published_date"))
                source, title = row.get("source"), row.get("title")
                if (row.get("issuer_code") != code or row.get("evidence_kind") != "announcement_metadata"
                        or published is None or not target - timedelta(days=2) <= published <= target
                        or not isinstance(title, str) or not title.strip() or len(title) > 500
                        or not _ann_url(row.get("url"), source, code)):
                    excluded += 1
                    continue
                record = dict(issuer_code=code, title=title, published_date=published.isoformat(), source=source,
                              url=row["url"], evidence_kind="announcement_metadata", fetched_at=response["fetched_at"],
                              issuer_name=shared["instruments"][code]["name"])
                if record not in records:
                    records.append(record)
        except _InvalidFacts:
            raise
        except Exception:
            failed += 1
    records.sort(key=lambda row: (row["published_date"], row["issuer_code"], row["url"]), reverse=True)
    coverage = dict(scope="bounded_public_announcement_metadata", scope_count=len(codes), scanned_count=scanned,
        unscanned_count=max(0, len(codes) - scanned), failed_count=failed, verified_count=verified,
        excluded_record_count=excluded, window_days=3, limit_per_code=10, scanned_codes=codes[:30])
    evidence.append(store.capture_payload("announcement_providers", "announcement_metadata",
        dict(records=records, coverage=coverage, instruments=shared["instruments"]), fetched_at=clock(), business_date=target, provenance="provider_normalized"))
    warnings = ["bounded_sample_not_no_company_risk", "metadata_only_fulltext_amount_sentiment_unverified"]
    if failed:
        warnings.append("announcement_partial_source_failure")
    if coverage["unscanned_count"]:
        warnings.append("announcement_scope_partially_unscanned")
    if excluded:
        warnings.append("announcement_unverified_records_excluded")
    if not verified:
        raise _InvalidFacts("announcement_sources_unavailable", coverage=coverage, warnings=warnings)
    return dict(records=records, as_of=target.isoformat(), research_context=shared), coverage, warnings


def produce_auxiliary(category_id, *, business_date, config, evidence_dir, collector_context=None,
                      fetcher_manager=None, hotspot_service=None, announcement_fetcher=None, now=None, progress=None):
    """Produce current-day bounded facts; injectable dependencies never send."""
    target = parse_date(business_date)
    if target is None:
        raise ValueError("auxiliary_business_date_invalid")
    evidence = []

    def blocked(code, *, coverage=None, warnings=()):
        return ProducerResult(category_id, target.isoformat(), "blocked", coverage=coverage or {}, evidence=evidence,
                              warnings=list(warnings), blockers=[code])

    if category_id not in CATEGORIES:
        return blocked("unsupported_auxiliary_category")

    def clock():
        current = now() if callable(now) else now
        current = datetime.now(SHANGHAI) if current is None else current
        if not isinstance(current, datetime) or current.utcoffset() is None:
            raise _InvalidFacts("auxiliary_clock_invalid")
        current = current.astimezone(SHANGHAI)
        if current.date() != target:
            raise _InvalidFacts("auxiliary_current_business_day_only")
        return current

    store = EvidenceStore(evidence_dir)
    try:
        clock()
        if category_id == "etf_rotation":
            result, coverage, warnings = _etf(config, target, clock, store, evidence, fetcher_manager, progress)
        elif category_id == "market_radar":
            result, coverage, warnings = _radar(target, clock, store, evidence, hotspot_service,
                                               fetcher_manager, collector_context, progress)
        else:
            result, coverage, warnings = _announcements(config, target, clock, store, evidence,
                announcement_fetcher, collector_context, progress)
        clock()
        _progress(progress, stage="computed", category_id=category_id)
        return ProducerResult(category_id, target.isoformat(), "degraded" if warnings else "ready", "auxiliary-v1",
            json_safe(result), json_safe(coverage), evidence, sorted(set(warnings)))
    except _Interrupted as exc:
        raise exc.error
    except _InvalidFacts as exc:
        return blocked(str(exc), coverage=exc.coverage, warnings=exc.warnings)
    except Exception:
        # Provider errors may contain credentials; only a fixed boundary code.
        return blocked("auxiliary_production_failed")
