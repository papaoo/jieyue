"""Verified saved-source composition shared by preview and explicit revisions."""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from src.services.research_delivery.aggregation import canonical, content_hash
from src.services.research_delivery.evidence import business_date, canonical_code, finite_number
from src.services.research_delivery.market_context import _read, _stamp, build_market_context, empty_market_context, instrument

SHANGHAI = ZoneInfo('Asia/Shanghai')
SOURCE_KEYS = {'category_id', 'business_date', 'state', 'strategy_version', 'result', 'coverage', 'evidence', 'warnings', 'blockers'}


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('preview_duplicate_json_key')
            value[key] = item
        return value

    def reject(_):
        raise ValueError('preview_nonfinite_json')

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def _source(row):
    source = _json(row['result_json'])
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS or content_hash(source) != row['input_hash']:
        raise ValueError('preview_batch_hash_invalid')
    if any(source.get(key) != row[key] for key in ('business_date', 'category_id', 'strategy_version')):
        raise ValueError('preview_batch_binding_invalid')
    if (business_date(source['business_date']) is None or not source['strategy_version']
            or not isinstance(source['result'], dict) or not isinstance(source['coverage'], dict)
            or any(not isinstance(source[key], list) for key in ('evidence', 'warnings', 'blockers'))):
        raise ValueError('preview_batch_schema_invalid')
    return source


def read_batches(db_path, batch_id):
    """Raw readonly snapshot, not the schema-creating/recovery repository."""
    path = Path(db_path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM research_automation_batches WHERE id=?', (batch_id,)).fetchone()
        if row is None:
            raise ValueError('preview_batch_not_found')
        selected = _source(row)
        if selected['state'] not in {'ready', 'degraded'} or selected['blockers']:
            raise ValueError('preview_batch_incomplete')
        rows = db.execute('SELECT * FROM research_automation_batches WHERE business_date=? ORDER BY created_at,id', (row['business_date'],)).fetchall()
        related, bindings = [], []
        for other in rows:
            source = _source(other)
            if source['state'] in {'ready', 'degraded'} and not source['blockers']:
                related.append(source)
                bindings.append(dict(id=other['id'], input_hash=other['input_hash']))
        metadata = {key: row[key] for key in ('id', 'business_date', 'category_id', 'strategy_version', 'input_hash', 'created_at')}
        metadata['verified_batches'] = bindings
        return selected, related, metadata


def _manifest(path, day):
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 2_000_000:
        raise ValueError('preview_manifest_too_large')
    manifest = _json(path.read_bytes())
    if (not isinstance(manifest, dict) or manifest.get('purpose') != 'no_send_acceptance'
            or manifest.get('business_date') != day or not isinstance(manifest.get('captures'), list)):
        raise ValueError('preview_manifest_invalid')
    return manifest, path.parent


def _capture(entry, directory, day, now, *, source, dataset):
    if (not isinstance(entry, dict) or entry.get('source') != source or entry.get('dataset') != dataset
            or entry.get('business_date') != day or entry.get('provenance') != 'provider_normalized'):
        raise ValueError('preview_capture_metadata_invalid')
    path = Path(entry['path']).resolve(strict=True)
    if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('preview_capture_path_invalid')
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != entry.get('sha256'):
        raise ValueError('preview_capture_hash_invalid')
    capture = _json(raw)
    if any(capture.get(key) != entry.get(key) for key in ('source', 'dataset', 'business_date', 'fetched_at', 'provenance')):
        raise ValueError('preview_capture_binding_invalid')
    _stamp(capture.get('fetched_at'), business_date(day), now)
    return capture['payload']


def saved_context(related, *, day, now, evidence_dir=None, reference_manifest=None, background_manifest=None, bindings=()):
    """Compose verified historical captures without calling current-day loaders.

    The real generation clock is only an upper bound. _read/build keep each
    capture's original business date and actual same-day observation timestamp.
    """
    import pandas as pd

    target = business_date(day)
    context = empty_market_context(target)
    reference = reference_entry = daily = daily_entry = None
    extra, extra_evidence = [], []
    if reference_manifest:
        manifest, directory = _manifest(reference_manifest, day)
        for entry in manifest['captures']:
            if entry.get('endpoint') == 'stock_basic':
                reference, reference_entry = _read(entry, 'stock_basic', dict(exchange='', list_status='L'), target, directory, now)
            elif entry.get('dataset') == 'etf_names':
                payload = _capture(entry, directory, day, now, source='instrument_names', dataset='etf_names')
                for row in payload['instruments']:
                    if not canonical_code(row.get('code')) or row.get('asset_type') != 'etf':
                        raise ValueError('preview_instrument_invalid')
                    observed = _stamp(row.get('observed_at'), target, now)
                    extra.append(instrument(row['code'], row.get('name'), asset_type='etf', source=row['source'],
                        observed_at=observed, as_of=day, evidence_id=entry['sha256']))
                extra_evidence.append(entry)
    if evidence_dir:
        directory = Path(evidence_dir).resolve(strict=True)
        for batch in related:
            for entry in batch['evidence']:
                endpoint = entry.get('endpoint')
                if endpoint not in {'stock_basic', 'daily'}:
                    continue
                params = dict(exchange='', list_status='L') if endpoint == 'stock_basic' else dict(trade_date=target.strftime('%Y%m%d'))
                if entry.get('params') != params:
                    continue  # per-code historical daily evidence is not the daily snapshot
                if endpoint == 'stock_basic' and reference is None:
                    reference, reference_entry = _read(entry, endpoint, params, target, directory, now)
                elif endpoint == 'daily' and daily is None:
                    table, verified = _read(entry, endpoint, params, target, directory, now)
                    coverage = batch['coverage']
                    population = coverage.get('independent_universe_count', 0)
                    ratio = coverage.get('daily_response_coverage', 0)
                    if (type(population) is not int or population <= 0 or len(table) != coverage.get('daily_response_count')
                            or type(ratio) not in (int, float) or not .95 <= ratio <= 1
                            or abs(len(table) / population - ratio) > 1e-9):
                        raise ValueError('preview_daily_coverage_invalid')
                    daily, daily_entry = table, verified
    if reference is not None:
        # Coverage must be relative to the reference actually being composed.
        if daily is not None and not .95 <= len(daily) / len(reference) <= 1:
            raise ValueError('preview_reference_coverage_invalid')
        built = build_market_context(reference, daily if daily is not None else pd.DataFrame(columns=['ts_code', 'trade_date']),
            business_date=target, fetched_at=(daily_entry or reference_entry)['fetched_at'], source='tushare')
        context.update(built)
        for identity in context['instruments'].values():
            identity.update(observed_at=reference_entry['fetched_at'], fetched_at=reference_entry['fetched_at'], evidence_id=reference_entry['sha256'])
        context['evidence'] = [reference_entry] + ([daily_entry] if daily_entry else [])
        if context['industries']:
            context['industries'].update(membership_observed_at=reference_entry['fetched_at'], evidence_ids=[reference_entry['sha256'], daily_entry['sha256']])
    for identity in extra:
        context['instruments'][identity['code']] = identity
    context['evidence'].extend(extra_evidence)
    if background_manifest:
        manifest, directory = _manifest(background_manifest, day)
        if not any(row['id'] == manifest.get('source_batch_id') and row['input_hash'] == manifest.get('source_input_hash') for row in bindings):
            raise ValueError('preview_background_source_binding_invalid')
        source = next(batch for batch in related if content_hash(batch) == manifest['source_input_hash'])
        eligible = {row['code'] for row in source['result'].get('picks', [])}
        from src.services.research_delivery.research_context import FIELDS, _dated_data, _public_url
        from src.services.research_delivery.auxiliary_producers import _ann_url
        for entry in manifest['captures']:
            payload = _capture(entry, directory, day, now, source='research_context', dataset='candidate_background')
            code, background = payload['issuer_code'], payload['background']
            if (payload.get('version') != 'candidate-background-v1' or code not in eligible
                    or not canonical_code(code) or background.get('issuer_code') != code or background.get('business_date') != day):
                raise ValueError('preview_background_entity_invalid')
            for domain in FIELDS:
                block = background.get(domain)
                if not block:
                    continue
                data = block.get('data', {})
                if domain == 'valuation' and block.get('source') == 'screening_snapshot' and 'fetched_at' not in block:
                    # Task 2's snapshot PE/PB is not an independent provider
                    # observation. Bind it to that capture's original candidate;
                    # do not invent a provider timestamp or trust attached values.
                    rows = [row for row in source['result']['picks'] if row.get('code') == code]
                    if len(rows) != 1:
                        raise ValueError('preview_snapshot_valuation_binding_invalid')
                    expected = {}
                    for field in ('pe_ratio', 'pb_ratio'):
                        try:
                            expected[field] = finite_number(rows[0][field])
                        except (KeyError, TypeError, ValueError, OverflowError):
                            continue
                    if not expected or data != dict(expected, as_of=day) or block.get('status') != 'available':
                        raise ValueError('preview_snapshot_valuation_binding_invalid')
                else:
                    _stamp(block.get('fetched_at'), target, now)
                if _dated_data(dict(data, issuer_code=code), domain, code, target) != data:
                    raise ValueError('preview_background_domain_invalid')
            for domain in ('news', 'announcements'):
                leads = background.get(domain, [])
                if not isinstance(leads, list):
                    raise ValueError('preview_background_leads_invalid')
                for lead in leads:
                    if not isinstance(lead, dict):
                        raise ValueError('preview_background_lead_invalid')
                    _stamp(lead.get('fetched_at'), target, now)
                    published, title = business_date(lead.get('published_date')), lead.get('title')
                    if (lead.get('issuer_code') != code or published is None
                            or not target - timedelta(days=2) <= published <= target
                            or not isinstance(title, str) or not 0 < len(title) <= 500):
                        raise ValueError('preview_background_lead_binding_invalid')
                    if domain == 'news':
                        name = next(row.get('name') for row in source['result']['picks'] if row.get('code') == code)
                        if (lead.get('source') != 'search' or lead.get('evidence_kind') != 'company_news_lead'
                                or not _public_url(lead.get('url')) or not (re.search(r'(?<!\d)' + re.escape(code) + r'(?!\d)', title)
                                    or isinstance(name, str) and name and name != code and name in title)):
                            raise ValueError('preview_background_news_contract_invalid')
                    elif lead.get('evidence_kind') != 'announcement_metadata' or not _ann_url(lead.get('url'), lead.get('source'), code):
                        raise ValueError('preview_background_announcement_contract_invalid')
            context['background'][code] = deepcopy(background) | {'evidence_id': entry['sha256']}
            context['evidence'].append(entry)
    return context


def augmented_source(source, related, *, top_n, context=None):
    """One immutable saved snapshot supplies preview and revision facts."""
    frozen = deepcopy(source)
    day = source['business_date']
    if context is not None:
        frozen['result']['research_context'] = deepcopy(context)
        frozen['evidence'].extend(context['evidence'])
    context = frozen['result'].setdefault('research_context', empty_market_context(business_date(day)))
    compared, overlaps = set(), {}
    for other in related:
        if (other['business_date'] == day and other['state'] in {'ready', 'degraded'} and not other['blockers']
                and isinstance(other['result'].get('picks'), list)):
            compared.add(other['category_id'])
            for row in other['result']['picks'][:top_n]:
                overlaps.setdefault(row['code'], set()).add(other['category_id'])
    context.update(compared_strategies=sorted(compared), cross_strategy={code: sorted(categories) for code, categories in overlaps.items()})
    return frozen


def verified_source(row, *, now, evidence_cache=None):
    """Validate original-date observations, using real time only as upper bound.

    Cache is local to one validation operation, never reused across calls.
    """
    from src.services.research_delivery.formal_publisher import FormalPublisher
    source = _source(row)
    day = business_date(source['business_date'])
    if day > now.astimezone(SHANGHAI).date() or source['state'] not in {'ready', 'degraded'} or source['blockers']:
        raise ValueError('saved_source_incomplete')
    FormalPublisher._coverage(source)
    FormalPublisher._rows(source)
    if not source['evidence']:
        raise ValueError('saved_evidence_missing')
    cache = evidence_cache if evidence_cache is not None else set()
    for entry in source['evidence']:
        key = canonical(entry)
        if key in cache:
            continue
        digest = entry.get('sha256')
        if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or entry.get('evidence_id') != digest or entry.get('business_date') != source['business_date']):
            raise ValueError('saved_evidence_binding_invalid')
        _stamp(entry.get('fetched_at'), day, now)
        path = Path(entry['path']).resolve(strict=True)
        if path.name != digest + '.json' or not path.is_file() or path.stat().st_size > 10_000_000:
            raise ValueError('saved_evidence_path_invalid')
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != digest:
            raise ValueError('saved_evidence_hash_invalid')
        payload = _json(raw)
        if any(payload.get(k) != entry.get(k) for k in ('source', 'fetched_at', 'business_date')):
            raise ValueError('saved_evidence_metadata_invalid')
        if not isinstance(entry.get('source'), str) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}', entry['source']):
            raise ValueError('saved_evidence_source_invalid')
        for field in ('endpoint', 'dataset'):
            if field in entry and (payload.get(field) != entry[field] or not re.fullmatch('[a-z_]{1,80}', entry[field])):
                raise ValueError('saved_evidence_metadata_invalid')
        cache.add(key)
    return source
