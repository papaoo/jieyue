"""Read saved screening/intelligence only; never enrich, rank, collect or infer W1."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, inspect, func, or_

from src.storage import ScreeningRun, IntelligenceItem, utc_naive_now
from src.repositories.research_delivery_repository import ManagementError

METRICS = ("score", "screen_score", "price", "change_pct", "amount", "total_mv", "turnover_rate", "volume_ratio", "pe_ratio", "pb_ratio", "change_60d", "signal_score", "industry_rank", "industry_change_pct", "industry_heat_score", "concept_heat_score", "board_heat_score", "breakout_20d_pct", "range_20d_pct", "volume_ratio_20d", "volatility_20d_pct", "max_drawdown_20d_pct", "atr_20_pct", "daily_quality_score", "risk_score", "risk_penalty", "portfolio_penalty")
RISK_FIELDS = ("risk_summary", "risk_level", "risk_flags", "risk_score", "risk_penalty", "excluded_by_risk", "portfolio_flags", "portfolio_penalty", "llm_risks", "llm_invalidators", "llm_watch_items", "deep_analysis_risk_flags", "daily_quality_flags")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def safe_text(value, limit=600):
    if not isinstance(value, (str, int, float, bool)):
        return ""
    text = str(value)[:limit]
    # Never surface exception URLs, bearer/API-key-looking fields or private holdings.
    text = re.sub(r"https?://\S+", "[link omitted]", text)
    text = re.sub(r"(?i)(?:sk-[A-Za-z0-9_-]+|bearer\s+\S+|(?:api[_ -]?key|token|secret|password)\s*[:=]\s*\S+)", "[redacted]", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if re.search(r"(?i)(private_holdings|personal holdings|持仓数量|持仓成本|账户余额)", text):
        return "[private data omitted]"
    return text


def safe_url(value):
    if not isinstance(value, str) or len(value) > 1000 or re.search(r"[\s\x00-\x20<>\[\]()\\\"'`]", value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if parsed.port not in (None, 80, 443) or parsed.hostname.lower() in ("localhost", "open.feishu.cn"):
            return None
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return None
        except ValueError:
            if "." not in parsed.hostname or parsed.hostname.endswith((".local", ".internal")):
                return None
        if re.search(r"(?i)(token|secret|password|api[_-]?key|sk-|/hook/)", value):
            return None
        return value
    except ValueError:
        return None


def _saved_reasons(value):
    if not isinstance(value, list):
        return []
    return [{key: safe_text(item[key], 400) for key in ("code", "text", "source", "quality", "value") if key in item}
            for item in value[:12] if isinstance(item, dict)]


def _list(value):
    return value if isinstance(value, list) else []


class ResearchAggregator:
    def __init__(self, repository):
        self.repository = repository

    def sources(self, strategy_id):
        with self.repository.session_factory() as session:
            if not inspect(session.get_bind()).has_table(ScreeningRun.__tablename__):
                return []
            rows = session.scalars(select(ScreeningRun).where(ScreeningRun.strategy == strategy_id, ScreeningRun.market == "cn").order_by(ScreeningRun.created_at.desc()).limit(50))
            return [{"source_run_id": row.run_id, "strategy_id": row.strategy, "market": row.market,
                     "created_at": row.created_at, "candidate_count": row.candidate_count,
                     "snapshot_count": row.snapshot_count, "snapshot_source": safe_text(row.snapshot_source, 100),
                     "business_date": None, "w1_verified": False} for row in rows]

    def freeze(self, subscription, source_run_id):
        with self.repository.session_factory() as session:
            if not inspect(session.get_bind()).has_table(ScreeningRun.__tablename__):
                raise ManagementError("not_found", "Saved screening source not found", 404)
            row = session.scalar(select(ScreeningRun).where(ScreeningRun.run_id == source_run_id))
            if row is None:
                raise ManagementError("not_found", "Saved screening source not found", 404)
            if row.strategy != subscription["strategy_id"] or row.market != "cn":
                raise ManagementError("source_mismatch", "Source strategy or market does not match subscription", 409)
            raw_snapshot = row.result_json
            try:
                source = json.loads(raw_snapshot)
            except (TypeError, ValueError):
                raise ManagementError("invalid_source", "Saved screening result is unreadable", 422) from None
            if not isinstance(source, dict):
                raise ManagementError("invalid_source", "Saved screening result is not an object", 422)
            if (source.get("strategy") is not None and source["strategy"] != row.strategy) or (source.get("market") is not None and source["market"] != "cn"):
                raise ManagementError("source_mismatch", "Saved result metadata does not match its source", 409)
            blockers = ["w1_unverified", "business_date_unknown", "universe_and_parameters_unverified"]
            if not source.get("strategy") or not source.get("market") or not source.get("strategy_version"):
                blockers.append("source_metadata_missing")
            missing = []
            candidates = source.get("candidates")
            if not isinstance(candidates, list):
                candidates = []
                blockers.append("candidates_missing")
            if not candidates:
                blockers.append("empty_coverage_unverified")
            picks = []
            seen = set()
            for item in candidates[:subscription["top_n"]]:
                if not isinstance(item, dict) or not isinstance(item.get("code"), str) or not re.fullmatch(r"[0-9]{6}", item["code"]):
                    missing.append("candidate_code_invalid")
                    continue
                code = item["code"]
                if code in seen:
                    missing.append("duplicate_candidate_code")
                    continue
                seen.add(code)
                raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
                metrics, gaps = {}, []
                for key in METRICS:
                    value = item[key] if key in item else raw.get(key)
                    valid = type(value) in (int, float) and math.isfinite(value)
                    metrics[key] = value if valid else None
                    if value is not None and not valid:
                        gaps.append("invalid_metric:" + key)
                    if key in item and key in raw and item[key] != raw[key]:
                        gaps.append("conflicting_metric:" + key)
                    if key == "price" and value == 0:
                        gaps.append("price_zero_unverified")
                risks = []
                for origin, data in (("saved_candidate", item), ("saved_raw", raw)):
                    for key in RISK_FIELDS:
                        value = data.get(key)
                        values = value if isinstance(value, list) else [value]
                        for value in values[:20]:
                            if value is not None and safe_text(value):
                                risks.append({"field": key, "source": origin, "kind": "prior_model_inference" if key.startswith("llm_") or key.startswith("deep_analysis_") else "saved_risk", "text": safe_text(value)})
                context = item.get("dsa_context") if isinstance(item.get("dsa_context"), dict) else {}
                for warning in _list(context.get("warnings"))[:10]:
                    risks.append({"field": "warnings", "source": "saved_context", "kind": "saved_risk", "text": safe_text(warning)})
                if not risks:
                    gaps.append("risk_information_unknown")
                pick = {"code": code, "name": safe_text(item.get("name"), 100), "rank": item.get("rank") if type(item.get("rank")) is int else None,
                        "metrics": metrics, "why_selected": _saved_reasons(item.get("why_selected")), "why_now": _saved_reasons(item.get("why_now")),
                        "risks": risks, "missing": gaps}
                picks.append(pick)
            evidence = self._evidence(session, source_run_id, picks, candidates)
            counts = {key: source.get(key) if type(source.get(key)) is int and source[key] >= 0 else None for key in ("snapshot_count", "after_filter_count", "candidate_count", "daily_enrich_count")}
            if any(counts[key] is None for key in ("snapshot_count", "after_filter_count")):
                missing.append("coverage_unknown")
            source_errors = source.get("source_errors")
            error_count = len(source_errors) if isinstance(source_errors, (dict, list)) else int(bool(source_errors))
            if error_count:
                blockers.append("source_errors_present")
            global_risks = []
            for key in ("warnings", "degradation", "portfolio_concentration_notes", "llm_portfolio_risk"):
                value = source.get(key)
                for part in (value if isinstance(value, list) else [value])[:20]:
                    if safe_text(part):
                        global_risks.append({"field": key, "kind": "prior_model_inference" if key.startswith("llm_") else "saved_risk", "text": safe_text(part)})
            envelope = {"source_run_id": source_run_id, "source_hash": hashlib.sha256(raw_snapshot.encode()).hexdigest(),
                        "source_created_at": row.created_at.isoformat(), "strategy_id": row.strategy,
                        "strategy_version": safe_text(source.get("strategy_version"), 100) or None, "market": "cn", "business_date": None,
                        "ranking_mode": safe_text(source.get("ranking_mode"), 100) or "unknown", "llm_ranked": source.get("llm_ranked") if type(source.get("llm_ranked")) is bool else None,
                        "top_n": subscription["top_n"], "coverage": counts, "candidates": picks, "evidence": evidence,
                        "source_options": {key: source.get(key) if type(source.get(key)) is bool else None for key in ("risk_enabled", "portfolio_diversity_enabled", "daily_enriched", "deep_analysis_requested")},
                        "risks": global_risks, "missing": missing, "source_errors": {"count": error_count, "classification": "stored_source_error" if error_count else None},
                        "blockers": blockers, "w1_verified": False}
            return envelope, raw_snapshot

    def _evidence(self, session, run_id, picks, original):
        evidence, seen = [], set()
        now = utc_naive_now()
        def add(entry):
            key = (entry["issuer_code"], entry["url"], entry["published_at"], entry["title"], entry["summary"])
            if key not in seen:
                evidence.append(entry)
                seen.add(key)
        if inspect(session.get_bind()).has_table(IntelligenceItem.__tablename__):
            for pick in picks:
                rows = session.scalars(select(IntelligenceItem).where(IntelligenceItem.scope_type == "symbol", func.lower(IntelligenceItem.scope_value) == pick["code"], IntelligenceItem.market == "cn",
                    IntelligenceItem.fetched_at >= now - timedelta(days=30), or_(IntelligenceItem.published_at.is_(None), IntelligenceItem.published_at.between(now - timedelta(days=30), now))).order_by(IntelligenceItem.published_at.desc(), IntelligenceItem.id).limit(10))
                for row in rows:
                    add({"id": "intelligence:" + str(row.id), "issuer_code": pick["code"], "issuer_status": "exact_stored_symbol",
                         "title": safe_text(row.title, 250), "summary": safe_text(row.summary, 700), "url": safe_url(row.url),
                         "source_name": safe_text(row.source_name, 100), "source_type": safe_text(row.source_type, 32),
                         "published_at": row.published_at.isoformat() if row.published_at else None,
                         "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None, "date_status": "stored_publication_date" if row.published_at else "unknown"})
        allowed_codes = {p["code"] for p in picks}
        for candidate in original[:20]:
            if not isinstance(candidate, dict) or candidate.get("code") not in allowed_codes:
                continue
            for kind in ("dsa_news", "dsa_events"):
                values = candidate.get(kind)
                if not isinstance(values, list):
                    context = candidate.get("dsa_context") if isinstance(candidate.get("dsa_context"), dict) else {}
                    stored = context.get("news" if kind == "dsa_news" else "events")
                    values = stored.get("results") if isinstance(stored, dict) else stored
                if not isinstance(values, list):
                    continue
                for index, entry in enumerate(values[:5]):
                    if not isinstance(entry, dict):
                        continue
                    # Candidate enclosure is not issuer/date proof; preserve as unverified context.
                    add({"id": f"screening:{run_id}:{candidate['code']}:{kind}:{index}", "issuer_code": candidate["code"], "issuer_status": "unverified_candidate_context",
                         "title": safe_text(entry.get("title"), 250), "summary": safe_text(entry.get("snippet"), 700), "url": safe_url(entry.get("url")),
                         "source_name": safe_text(entry.get("source"), 100), "source_type": "saved_search",
                         "published_at": safe_text(entry.get("published_date"), 40) or None, "fetched_at": safe_text(entry.get("retrieved_at"), 40) or None,
                         "date_status": "unverified" if entry.get("published_date") else "unknown"})
        return evidence[:100]
