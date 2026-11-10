"""Scoped current-day facts from raw tables or hash-verified local evidence."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import pandas as pd

from data_provider.tushare_fact_normalizers import industry_facts, number
from src.data.stock_mapping import is_meaningful_stock_name
from src.services.research_delivery.evidence import business_date as parse_date, finite_number


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _stamp(value, target, now=None):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if (not isinstance(value, datetime) or value.utcoffset() is None
            or value.astimezone(SHANGHAI).date() != target or (now is not None and value > now)):
        raise ValueError("market_context_timestamp_invalid")
    return value.isoformat()


def empty_market_context(target):
    return dict(version="research-context-v1", business_date=target.isoformat(), as_of=target.isoformat(),
        instruments={}, industries={}, market_breadth={}, background={}, cross_strategy={},
        compared_strategies=[], statuses={}, industry_status="unavailable", missing=[], rejections=[], evidence=[])


def instrument(code, name, *, asset_type, source, observed_at, industry=None, **metadata):
    known = isinstance(name, str) and len(name) <= 120 and is_meaningful_stock_name(name.strip(), code)
    return dict(code=code, name=name.strip() if known else None, asset_type=asset_type,
        industry=industry if isinstance(industry, str) and industry.strip() else None,
        source=source, observed_at=observed_at, fetched_at=observed_at,
        status="available" if known else "unknown", name_status="resolved" if known else "unknown", **metadata)


def _identities(table):
    if (not isinstance(table, pd.DataFrame) or not table.columns.is_unique or "ts_code" not in table
            or table.ts_code.duplicated().any() or any(not isinstance(c, str)
                or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", c) for c in table.ts_code)
            or table.ts_code.str[:6].duplicated().any()):
        raise ValueError("market_context_identity_invalid")


def build_market_context(reference, daily, *, business_date, fetched_at, source, canonical_only=False):
    """Keep existing industry aggregate definitions; names are reference observations."""
    target = parse_date(business_date)
    if target is None or source != "tushare":
        raise ValueError("market_context_source_or_date_invalid")
    observed = _stamp(fetched_at, target)
    _identities(reference)
    _identities(daily)
    if ("trade_date" not in daily or any(parse_date(v) != target for v in daily.trade_date)
            or not set(daily.ts_code) <= set(reference.ts_code)):
        raise ValueError("market_context_daily_date_or_universe_invalid")
    result = empty_market_context(target)
    result["fetched_at"] = observed
    for row in reference.to_dict("records"):
        code = row["ts_code"][:6]
        result["instruments"][code] = instrument(code, row.get("name"), asset_type="stock",
            industry=row.get("industry"), source="tushare.stock_basic", observed_at=observed,
            ts_code=row["ts_code"], as_of=target.isoformat())
    normalized = daily.copy()
    normalized["trade_date"] = normalized.trade_date.map(lambda value: parse_date(value).strftime("%Y%m%d"))
    # Historical callers keep display-name aggregates. Strict live v2 cannot
    # promote the stock_basic string into a verified taxonomy membership.
    # The published canonical membership adapter is deliberately separate work.
    if not canonical_only:
        result["industries"], _ = industry_facts(reference, normalized, pd.DataFrame(), target.strftime("%Y%m%d"))
    if result["industries"]:
        result["industries"]["observed_at"] = observed
        # Same current-membership / observed-daily join as industry_facts;
        # attach directions without changing its means, coverage or ranking.
        members = reference[["ts_code", "industry"]].copy()
        members["industry"] = members.industry.fillna("").astype(str).str.strip()
        constituents = normalized[["ts_code", "pct_chg"]].merge(members, on="ts_code", how="left", validate="one_to_one")
        directions = {}
        for name, group in constituents.loc[constituents.industry.fillna("").ne("")].groupby("industry"):
            returns = group.pct_chg.map(number).dropna()
            directions[name] = dict(advancers=int(returns.gt(0).sum()), decliners=int(returns.lt(0).sum()),
                flat=int(returns.eq(0).sum()), unknown_count=len(group) - len(returns), source="computed_from_tushare")
        for group in ("all", "top", "bottom"):
            for row in result["industries"][group]:
                row.update(directions[row["name"]])
        result["industry_status"] = "available"
    else:
        result["missing"].append('canonical_industry_unassessed' if canonical_only else
            ("industry_membership_unavailable" if "industry" not in reference else "industry_returns_unavailable"))
    result["statuses"]["industries"] = result["industry_status"]
    if not daily.empty and {"pct_chg", "amount"} <= set(daily):
        try:
            changes = [finite_number(v) for v in daily.pct_chg]
            amounts = [finite_number(v) for v in daily.amount]
            if min(amounts) < 0:
                raise ValueError("market_context_amount_invalid")
        except (TypeError, ValueError):
            result["missing"].append("market_breadth_metrics_invalid")
        else:
            result["market_breadth"] = dict(advancers=sum(v > 0 for v in changes), decliners=sum(v < 0 for v in changes),
                flat=sum(v == 0 for v in changes), observed_count=len(changes), turnover_yuan=sum(amounts) * 1000.,
                source="tushare.daily:observed_snapshot", as_of=target.isoformat(), observed_at=observed)
    return result


def _read(entry, endpoint, params, target, directory, now):
    if (not isinstance(entry, dict) or entry.get("source") != "tushare" or entry.get("endpoint") != endpoint
            or entry.get("params") != params or entry.get("business_date") != target.isoformat()):
        raise ValueError("market_context_evidence_metadata_invalid")
    path = Path(entry["path"]).resolve(strict=True)
    if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size > 10_000_000:
        raise ValueError("market_context_evidence_path_invalid")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != entry.get("sha256"):
        raise ValueError("market_context_evidence_hash_invalid")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("market_context_evidence_metadata_invalid")
    if any(data.get(field) != entry.get(field) for field in ("source", "endpoint", "params", "business_date", "fetched_at")):
        raise ValueError("market_context_evidence_metadata_invalid")
    _stamp(data.get("fetched_at"), target, now)
    table = pd.DataFrame(data["rows"], columns=data["columns"])
    _identities(table)
    if len(table) != entry.get("row_count"):
        raise ValueError("market_context_evidence_count_invalid")
    # Select metadata rather than propagating arbitrary caller fields.
    verified = dict(source="tushare", endpoint=endpoint, params=params, path=str(path),
        sha256=entry["sha256"], evidence_id=entry["sha256"], fetched_at=data["fetched_at"],
        business_date=target.isoformat(), row_count=len(table))
    return table, verified


def load_market_context(stock_batches, *, business_date, evidence_dir, now):
    """Ignore attached contexts: only exact same-day stored tables are trusted.

    Reference-only evidence can resolve announcement names. Industry and breadth
    additionally require a >=95% observed daily response against that reference.
    """
    target = parse_date(business_date)
    if target is None:
        raise ValueError("market_context_date_invalid")
    _stamp(now, target)
    result = empty_market_context(target)
    directory = Path(evidence_dir).resolve()
    strict_context = any(isinstance(batch, dict) and batch.get('business_date') == target.isoformat()
        and batch.get('state') in {'ready', 'degraded'} and batch.get('blockers') == []
        and isinstance(batch.get('coverage'), dict) and batch['coverage'].get('contract_version') == 2
        for batch in (stock_batches if isinstance(stock_batches, list) else []))
    for batch in stock_batches if isinstance(stock_batches, list) else []:
        if (not isinstance(batch, dict) or batch.get("business_date") != target.isoformat()
                or batch.get("state") not in {"ready", "degraded"} or batch.get("blockers") != []):
            result["rejections"].append("market_context_batch_not_current_verified")
            continue
        tables, references = {}, {}
        for endpoint, params in (("stock_basic", dict(exchange="", list_status="L")),
                                 ("daily", dict(trade_date=target.strftime("%Y%m%d")))):
            entries = batch.get("evidence", [])
            if not isinstance(entries, list):
                result["rejections"].append("market_context_evidence_metadata_invalid")
                entries = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("endpoint") != endpoint or entry.get("params") != params:
                    continue
                try:
                    tables[endpoint], references[endpoint] = _read(entry, endpoint, params, target, directory, now)
                    break
                except (KeyError, TypeError, ValueError, OSError):
                    result["rejections"].append("market_context_" + endpoint + "_evidence_rejected")
        if "stock_basic" not in tables:
            result["rejections"].append("market_context_reference_unavailable")
            continue
        reference = tables["stock_basic"]
        names = build_market_context(reference, pd.DataFrame(columns=["ts_code", "trade_date"]),
            business_date=target, fetched_at=references["stock_basic"]["fetched_at"], source="tushare")
        for value in names["instruments"].values():
            value["evidence_id"] = references["stock_basic"]["sha256"]
        result["instruments"].update(names["instruments"])
        if references["stock_basic"] not in result["evidence"]:
            result["evidence"].append(references["stock_basic"])
        try:
            daily = tables["daily"]
            coverage = batch["coverage"]
            universe = finite_number(coverage["independent_universe_count"])
            ratio = finite_number(coverage["daily_response_coverage"])
            if (universe != len(reference) or len(daily) != coverage["daily_response_count"]
                    or universe <= 0 or not .95 <= ratio <= 1 or abs(len(daily) / universe - ratio) > 1e-9):
                raise ValueError("market_context_coverage_invalid")
            current = build_market_context(reference, daily, business_date=target,
                fetched_at=references["daily"]["fetched_at"], source="tushare",
                canonical_only=strict_context)
            result.update(industries=current["industries"], industry_status=current["industry_status"],
                market_breadth=current["market_breadth"], fetched_at=current["fetched_at"], missing=current["missing"])
            if references["daily"] not in result["evidence"]:
                result["evidence"].append(references["daily"])
            if result["industries"]:
                result["industries"]["evidence_ids"] = [references[e]["sha256"] for e in ("stock_basic", "daily")]
                result["industries"]["membership_observed_at"] = references["stock_basic"]["fetched_at"]
                break
        except (KeyError, TypeError, ValueError):
            result["rejections"].append("market_context_daily_or_coverage_rejected")
    if not result["industries"] and not result["missing"]:
        result["missing"].append("industry_current_verified_facts_unavailable")
    result["missing"] = sorted(set(result["missing"]))
    result["rejections"] = sorted(set(result["rejections"]))
    result["statuses"]["industries"] = result["industry_status"]
    return result
