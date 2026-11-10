"""Trusted daily publications and fenced, durable single-attempt delivery.

The committed ``sending`` transition is the pause linearization point. A request
already in flight cannot be revoked. Unknown outcomes are never retried.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select

from src.notification_sender.feishu_sender import DeliveryOutcome, send_once
from src.repositories.automation_models import AutomationControl, AutomationJob, PublicationRelease, ResearchBatch
from src.repositories.research_delivery_models import DeliveryDestination, DeliveryOutbox, DeliverySubscription, ResearchPublication
from src.repositories.research_delivery_repository import ManagementError
from src.services.research_delivery.aggregation import canonical, content_hash
from src.services.research_delivery.credentials import CredentialError
from src.services.research_delivery.explanation import ExplanationService
from src.services.research_delivery.live_contracts import ProducerResult
from src.services.research_delivery.collection_checkpoint import (
    DEPENDENCY_VERSION, digest as collection_digest, strategy_catalog_hash, unpack_frame,
)
from src.services.research_delivery.publisher import ResearchPublisher
from src.services.research_delivery.render_formal import formal_envelope
from src.services.research_delivery.research_brief import FORMAT_VERSION, PROMPT_VERSION, build_brief_input, validate_brief_explanation
from src.services.research_delivery.brief_rendering import render_brief, frozen_payload
from src.services.research_delivery.timing import (
    PUBLICATION_WINDOW_END_TIME,
    PUBLICATION_WINDOW_START_TIME,
)


SHANGHAI = ZoneInfo('Asia/Shanghai')
PAYLOAD_BYTE_LIMIT = 16 * 1024
AUXILIARY = {'etf_rotation', 'market_radar', 'announcement_risk'}
TERMINAL = {'sent', 'rejected', 'delivery_unknown', 'suppressed'}


def _fail(code):
    raise ManagementError(code, 'Formal publication guard: ' + code, 409)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class FormalPublisher:
    def __init__(self, service, config, *, sender=None, clock=None):
        self.service, self.repository = service, service.repository
        self.config, self.clock = config, clock
        self.sender = sender or send_once
        self.explanations = ExplanationService(self.repository, config)

    def _now(self):
        stamp = self.clock() if self.clock else datetime.now(SHANGHAI)
        if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
            _fail('clock_invalid')
        return stamp.astimezone(SHANGHAI)

    @staticmethod
    def _utc(stamp):
        return stamp.astimezone(timezone.utc).replace(tzinfo=None)

    def _window(self, business_date):
        now = self._now()
        if business_date != now.date().isoformat():
            _fail('business_date_mismatch')
        if not PUBLICATION_WINDOW_START_TIME <= now.time() < PUBLICATION_WINDOW_END_TIME:
            _fail('outside_publication_window')
        return now

    def _version(self, category):
        if category in AUXILIARY:
            return 'auxiliary-v1'
        for row in self.service.strategies():
            if row['strategy_id'] == category:
                return row['version']
        _fail('category_unsupported')

    def _validate_source(self, source):
        if (not isinstance(source, dict) or set(source) != {'category_id', 'business_date', 'state',
                'strategy_version', 'result', 'coverage', 'evidence', 'warnings', 'blockers'}
                or not isinstance(source.get('state'), str) or source['state'] not in {'ready', 'degraded', 'blocked', 'skipped'}
                or not isinstance(source.get('category_id'), str)
                or not isinstance(source.get('result'), dict) or not isinstance(source.get('coverage'), dict)
                or any(not isinstance(source.get(key), list) for key in ('evidence', 'warnings', 'blockers'))
                or any(not isinstance(v, str) for key in ('warnings', 'blockers') for v in source[key])):
            _fail('invalid_batch')
        now = self._window(source['business_date'])
        if source['strategy_version'] != self._version(source['category_id']):
            _fail('strategy_version_changed')
        if source['state'] not in {'ready', 'degraded'} or source['blockers']:
            _fail('batch_blocked')
        self._coverage(source)
        self._evidence(source, now)
        self._rows(source)
        return now

    @staticmethod
    def _coverage(source):
        category, c = source['category_id'], source['coverage']
        def positive(key):
            return type(c.get(key)) is int and c[key] > 0
        if category == 'etf_rotation':
            valid = (positive('asset_count') and positive('valid_asset_count') and c['valid_asset_count'] == c['asset_count']
                and type(c.get('history_years')) is int and c['history_years'] == 1)
        elif category == 'market_radar':
            valid = (c.get('scope') in {'observed_snapshot_not_full_market', 'verified_theme_subset'}
                and all(type(c.get(k)) is bool for k in ('industry_available', 'concept_available')))
        elif category == 'announcement_risk':
            valid = (c.get('scope') == 'bounded_public_announcement_metadata' and positive('scope_count')
                and positive('scanned_count') and positive('verified_count')
                and all(type(c.get(k)) is int and c[k] >= 0 for k in ('unscanned_count', 'failed_count'))
                and c['scanned_count'] + c['unscanned_count'] == c['scope_count']
                and c['verified_count'] <= c['scanned_count'])
        else:
            calendar = c.get('calendar')
            valid = (c.get('scope') == 'restricted_complete_core_metric_eligible_universe'
                and c.get('calendar') == dict(exchange='SSE', business_date=source['business_date'], is_open=1)
                and isinstance(calendar, dict) and type(calendar.get('is_open')) is int
                and positive('independent_universe_count') and positive('eligible_count')
                and c['eligible_count'] <= c['independent_universe_count']
                and positive('required_count') and positive('valid_count') and c['valid_count'] == c['required_count']
                and type(c.get('technical_limit')) is int and 0 < c['technical_limit']
                and (c.get('contract_version') == 2 or c['technical_limit'] <= 100))
            if c.get('contract_version') == 2:
                scope = c.get('evaluation_scope', {})
                if not isinstance(scope, dict) or not isinstance(c.get('daily_evidence'), dict):
                    _fail('coverage_invalid')
                codes, evaluated = scope.get('codes'), c.get('evaluated_codes')
                evidence = c.get('daily_evidence', {})
                valid = (valid and c.get('strategy_dependency_version') == DEPENDENCY_VERSION
                    and all(type(c.get(k)) is int and c[k] >= 0 for k in
                        ('evaluation_scope_count', 'evaluated_count', 'pending_count', 'failed_count'))
                    and c.get('pending_count') == c.get('failed_count') == 0
                    and c.get('pending_codes') == c.get('failed_codes') == []
                    and c.get('evaluated_count') == c.get('evaluation_scope_count')
                    and c.get('evaluation_scope_count') == c.get('technical_planned_count') == c.get('snapshot_pass_count')
                    and c.get('technical_not_evaluated_count') == 0
                    and c.get('evaluation_scope_count') <= c.get('eligible_count')
                    and isinstance(codes, list) and all(isinstance(code, str) and re.fullmatch('[0-9]{6}', code) for code in codes)
                    and len(codes) == len(set(codes)) == c.get('evaluation_scope_count')
                    and evaluated == codes and set(evidence) == set(codes)
                    and scope.get('business_date') == source['business_date'] and scope.get('strategy_id') == category
                    and scope.get('dependency_version') == DEPENDENCY_VERSION
                    and all(isinstance(scope.get(k), str) and re.fullmatch('[0-9a-f]{64}', scope[k])
                            for k in ('snapshot_hash', 'strategy_hash', 'pool_hash'))
                    and c.get('scope_hash') == collection_digest(scope))
                if valid:
                    for code in codes:
                        refs = [e for e in source['evidence'] if e.get('params', {}).get('ts_code', '')[:6] == code]
                        valid = valid and any(e.get('endpoint') == 'daily' for e in refs) and any(
                            e.get('endpoint') == 'adj_factor' and e.get('sha256') == evidence[code] for e in refs)
            for family in ('daily', 'daily_basic'):
                ratio, count = c.get(family + '_response_coverage'), c.get(family + '_response_count')
                valid = (valid and _number(ratio) and .95 <= ratio <= 1 and positive(family + '_response_count')
                    and abs(count / c['independent_universe_count'] - ratio) < 1e-9)
        if not valid:
            _fail('coverage_invalid')

    @staticmethod
    def _evidence(source, now):
        if not source['evidence']:
            _fail('evidence_invalid')
        verified_payloads = {}
        for entry in source['evidence']:
            try:
                digest = entry['sha256']
                if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                    _fail('evidence_invalid')
                if entry.get('evidence_id') != digest or entry['business_date'] != source['business_date']:
                    _fail('evidence_invalid')
                stamp = datetime.fromisoformat(entry['fetched_at'])
                if stamp.utcoffset() is None or stamp.astimezone(SHANGHAI).date() != now.date() or stamp > now:
                    _fail('evidence_invalid')
                path = Path(entry['path'])
                if path.name != digest + '.json' or not path.is_file() or path.stat().st_size > 10_000_000:
                    _fail('evidence_invalid')
                raw = path.read_bytes()
                if sha256(raw).hexdigest() != digest:
                    _fail('evidence_invalid')
                payload = json.loads(raw)
                if any(payload.get(k) != entry.get(k) for k in ('source', 'fetched_at', 'business_date')):
                    _fail('evidence_invalid')
                if not isinstance(entry.get('source'), str) or not re.fullmatch('[A-Za-z0-9_.-]{1,80}', entry['source']):
                    _fail('evidence_invalid')
                for key in ('endpoint', 'dataset'):
                    if key in entry and (payload.get(key) != entry[key] or not re.fullmatch('[a-z_]{1,80}', entry[key])):
                        _fail('evidence_invalid')
                if 'params' in entry and payload.get('params') != entry['params']:
                    _fail('evidence_invalid')
                verified_payloads[digest] = payload
            except (KeyError, TypeError, ValueError, OSError):
                _fail('evidence_invalid')
        # Independently re-check the positive suspension proof and both original
        # parents. Current business_date is capture time, requested_date may be
        # an older missing history session, never a tradable placeholder bar.
        from data_provider.historical_reference import day
        from src.services.screening.eligibility import verified_suspension_proof
        for payload in verified_payloads.values():
            if payload.get('source') != 'baostock' or payload.get('dataset') != 'suspension_status':
                continue
            try:
                proof = payload['payload']
                code, requested = proof['ts_code'], day(proof['requested_date'])
                if requested > day(source['business_date']):
                    _fail('evidence_invalid')
                parents = []
                for endpoint, key in (('suspend_d', 'primary'), ('daily', 'daily')):
                    parent = verified_payloads[proof[key]]
                    params = parent['params']
                    if parent.get('source') != 'tushare' or parent.get('endpoint') != endpoint:
                        _fail('evidence_invalid')
                    if 'trade_date' in params:
                        if day(params['trade_date']) != requested:
                            _fail('evidence_invalid')
                    elif (params.get('ts_code') != code
                          or not day(params['start_date']) <= requested <= day(params['end_date'])):
                        _fail('evidence_invalid')
                    parents.append([dict(zip(parent['columns'], row)) for row in parent['rows']])
                events = [r for r in parents[0] if r['ts_code'] == code and day(r['trade_date']) == requested]
                verified_suspension_proof(events, parents[1], proof['records'], code=code, stamp=requested)
            except (KeyError, TypeError, ValueError):
                _fail('evidence_invalid')
        current_providers = {}
        for parent in verified_payloads.values():
            if (parent.get('source') == 'tushare' and parent.get('endpoint') == 'stock_basic'
                    and parent.get('params', {}).get('list_status') == 'L'):
                for row in parent.get('rows', []):
                    item = dict(zip(parent.get('columns', []), row))
                    provider = item.get('ts_code')
                    if isinstance(provider, str):
                        current_providers.setdefault(provider[:6], []).append(provider)
        for security, value in source['coverage'].get('scoring_eligibility', {}).items():
            if 'suspension_proof' in value:
                proof = value['suspension_proof']
                required = {'ts_code', 'requested_date', 'primary', 'daily', 'evidence_id'}
                if (not isinstance(proof, dict) or set(proof) != required
                        or not isinstance(security, str) or not re.fullmatch('[0-9]{6}', security)
                        or not isinstance(proof['ts_code'], str) or not re.fullmatch('[0-9]{6}\\.(SH|SZ)', proof['ts_code'])
                        or proof['ts_code'][:6] != security or proof['requested_date'] != source['business_date']
                        or value.get('eligible') is not False or value.get('reason') != 'verified_full_day_suspension'
                        or any(not isinstance(proof[k], str) or not re.fullmatch('[0-9a-f]{64}', proof[k])
                               for k in ('primary', 'daily', 'evidence_id'))):
                    _fail('evidence_invalid')
                # Bind the consumer key to the observed current listing, not
                # merely a syntactically plausible code borrowed from a proof.
                if (current_providers.get(security) != [proof['ts_code']]
                        or source['coverage'].get('listing_eligibility', {}).get(security, {}).get('exchange')
                           != {'SH': 'SSE', 'SZ': 'SZSE'}[proof['ts_code'][-2:]]
                        or not any(item.get('code') == security and item.get('reasons') == ['verified_full_day_suspension']
                                   for item in source['coverage'].get('excluded', []))):
                    _fail('evidence_invalid')
                captured = verified_payloads.get(proof.get('evidence_id'), {})
                if (captured.get('source') != 'baostock' or captured.get('dataset') != 'suspension_status'
                        or any(captured.get('payload', {}).get(k) != proof[k] for k in required - {'evidence_id'})
                        or value.get('evidence_id') != collection_digest(proof)):
                    _fail('evidence_invalid')
        if source['coverage'].get('contract_version') == 2 and source['category_id'] not in AUXILIARY:
            snapshots = [entry for entry in source['evidence'] if entry.get('source') == 'collection'
                         and entry.get('dataset') == 'frozen_snapshot']
            if len(snapshots) != 1:
                _fail('evidence_invalid')
            frozen = json.loads(Path(snapshots[0]['path']).read_bytes())['payload']
            if collection_digest(frozen) != source['coverage']['evaluation_scope']['snapshot_hash']:
                _fail('evidence_invalid')
            FormalPublisher._verified_scope(source, frozen)

    @staticmethod
    def _verified_scope(source, frozen):
        """Rebuild the plan from verified bytes, not mutually consistent claims."""
        from src.services.screening.config import Config
        from src.services.research_delivery.screening_preview import preview_strategy
        try:
            coverage = source['coverage']
            scope = coverage['evaluation_scope']
            target = date.fromisoformat(source['business_date'])
            runtime = Config()  # no env, providers, LLM or side effects
            if scope['strategy_hash'] != strategy_catalog_hash(runtime.strategies_dir, target):
                _fail('coverage_invalid')
            ids = scope['pool_strategy_ids']
            if (not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)
                    or ids != sorted(set(ids)) or source['category_id'] not in ids):
                _fail('coverage_invalid')
            snapshot = unpack_frame(frozen)
            if len(snapshot) != coverage['eligible_count']:
                _fail('coverage_invalid')
            plans = {}
            for key in ids:
                plan = preview_strategy(key, snapshot, trade_date=target, universe=tuple(snapshot.code),
                    calendar_verified=True, universe_verified=True, config=runtime, max_output=20)
                if plan.scope is None or any(not b.startswith('daily:') for b in plan.readiness.blockers):
                    _fail('coverage_invalid')
                plans[key] = plan
            expected = plans[source['category_id']]
            union = sorted({code for plan in plans.values() for code in plan.scope.technical_planned})
            if (scope['codes'] != list(expected.scope.technical_planned)
                    or coverage['required_count'] != expected.readiness.required_count
                    or scope['pool_hash'] != collection_digest(union)):
                _fail('coverage_invalid')
        except (KeyError, TypeError, ValueError, AttributeError):
            _fail('coverage_invalid')

    @staticmethod
    def _rows(source):
        category, result, day = source['category_id'], source['result'], source['business_date']
        if category not in AUXILIARY:
            rows = result.get('picks')
            if (result.get('strategy') != category or result.get('market') != 'cn' or result.get('ranking_mode') != 'factor'
                    or result.get('strategy_version') != source['strategy_version']
                    or result.get('llm_ranked') is not False or result.get('source_errors')):
                _fail('source_metadata_invalid')
            if not isinstance(rows, list) or len(rows) > 20:
                _fail('candidates_invalid')
            seen = set()
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get('code'), str)
                        or not re.fullmatch('[0-9]{6}', row['code']) or row['code'] in seen
                        or type(row.get('rank')) is not int or row['rank'] < 1
                        or not _number(row.get('final_score')) or not _number(row.get('screen_score'))):
                    _fail('candidates_invalid')
                seen.add(row['code'])
        elif category in {'etf_rotation', 'market_radar'}:
            rows = result.get('items')
            if not isinstance(rows, list) or not rows or result.get('as_of') != day:
                _fail('candidates_invalid')
            seen = set()
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                        or not re.fullmatch('[A-Za-z0-9:_-]{1,100}', row['id']) or row['id'] in seen
                        or row.get('as_of') != day or not isinstance(row.get('source'), str)
                        or not re.fullmatch('[A-Za-z0-9_.:-]{1,100}', row['source'])
                        or not isinstance(row.get('metrics'), dict) or not row['metrics']
                        or any(not _number(v) for v in row['metrics'].values())):
                    _fail('candidates_invalid')
                seen.add(row['id'])
                metrics = row['metrics']
                if category == 'etf_rotation':
                    if (not re.fullmatch('[15][0-9]{5}', row['id']) or not {'momentum', 'close'} <= set(metrics)
                            or metrics['close'] <= 0):
                        _fail('candidates_invalid')
                elif row['id'] == 'market:breadth':
                    counts = ('advancers', 'decliners', 'flat', 'observed_count')
                    if (not set((*counts, 'turnover_yuan')) <= set(metrics)
                            or any(type(metrics[k]) is not int or metrics[k] < 0 for k in counts)
                            or metrics['observed_count'] < 1 or metrics['turnover_yuan'] < 0
                            or sum(metrics[k] for k in counts[:3]) != metrics['observed_count']):
                        _fail('candidates_invalid')
                elif not re.fullmatch('(industry|concept):[0-9a-f]{16}', row['id']) or 'change_pct' not in metrics:
                    _fail('candidates_invalid')
            if category == 'etf_rotation':
                weights = result.get('target_weights')
                assumptions = result.get('execution_assumptions')
                if (not isinstance(weights, dict) or not weights or any(not isinstance(k, str) or not re.fullmatch('[15][0-9]{5}', k)
                        or not _number(v) or not 0 <= v <= 1 for k, v in weights.items())
                        or not isinstance(assumptions, dict) or assumptions.get('mode') != 'reference_only'):
                    _fail('source_metadata_invalid')
        else:
            rows = result.get('records')
            if not isinstance(rows, list) or result.get('as_of') != day:
                _fail('candidates_invalid')
            from src.services.research_delivery.auxiliary_producers import _ann_url
            for row in rows:
                try:
                    published = date.fromisoformat(row['published_date'])
                    if (not isinstance(row['issuer_code'], str) or not re.fullmatch('[0-9]{6}', row['issuer_code'])
                            or not isinstance(row['title'], str) or not 1 <= len(row['title']) <= 500
                            or row['evidence_kind'] != 'announcement_metadata'
                            or not date.fromisoformat(day) - timedelta(days=2) <= published <= date.fromisoformat(day)
                            or not _ann_url(row['url'], row['source'], row['issuer_code'])):
                        _fail('candidates_invalid')
                except (KeyError, TypeError, ValueError):
                    _fail('candidates_invalid')

    def store_batch(self, result):
        if not isinstance(result, ProducerResult):
            _fail('untrusted_source')
        try:
            source = result.to_dict()
        except (TypeError, ValueError):
            _fail('invalid_batch')
        now = self._validate_source(source)
        digest = content_hash(source)
        with self.repository.atomic() as session:
            row = session.scalar(select(ResearchBatch).where(ResearchBatch.input_hash == digest,
                ResearchBatch.business_date == source['business_date'], ResearchBatch.category_id == source['category_id']))
            if row is None:
                row = ResearchBatch(id=uuid.uuid4().hex, business_date=source['business_date'], category_id=source['category_id'],
                    strategy_version=source['strategy_version'], input_hash=digest, result_json=canonical(source),
                    created_at=self._utc(now), expires_at=self._utc(datetime.combine(now.date(), PUBLICATION_WINDOW_END_TIME, SHANGHAI)))
                session.add(row)
                session.flush()
            return self._batch_public(row)

    @staticmethod
    def _batch_public(row):
        return {k: getattr(row, k) for k in ('id', 'business_date', 'category_id', 'strategy_version', 'input_hash', 'created_at', 'expires_at')}

    def _batch(self, session, batch_id):
        row = session.get(ResearchBatch, batch_id)
        if row is None:
            _fail('batch_not_found')
        now = self._window(row.business_date)
        if row.expires_at <= self._utc(now):
            _fail('batch_expired')
        try:
            source = json.loads(row.result_json)
            if content_hash(source) != row.input_hash:
                _fail('batch_hash_changed')
        except (TypeError, ValueError):
            _fail('batch_hash_changed')
        if any(source.get(k) != getattr(row, k) for k in ('business_date', 'category_id', 'strategy_version')):
            _fail('batch_binding_changed')
        self._validate_source(source)
        return row, source

    def _control(self, session, lease_token, epoch=None):
        control = session.get(AutomationControl, 'main')
        if control is None or not control.enabled:
            _fail('automation_paused')
        if (not lease_token or control.lease_token != lease_token or control.lease_until is None
                or control.lease_until <= self._utc(self._now())):
            _fail('lease_lost')
        if epoch is not None and control.version != epoch:
            _fail('authorization_changed')
        return control

    def _lease(self, session, lease_token):
        """Prove mutation authority independently of pause/epoch validity."""
        control = session.get(AutomationControl, 'main')
        if (control is None or not lease_token or control.lease_token != lease_token
                or control.lease_until is None or control.lease_until <= self._utc(self._now())):
            _fail('lease_lost')
        return control

    @staticmethod
    def _configuration(session, subscription_id, batch, publication=None, release=None):
        sub = session.get(DeliverySubscription, subscription_id)
        target = session.get(DeliveryDestination, sub.destination_id) if sub else None
        if (sub is None or target is None or not sub.enabled or not target.enabled or not target.credential_ref
                or sub.strategy_id != batch.category_id or type(sub.top_n) is not int or not 1 <= sub.top_n <= 20
                or sub.llm_mode not in {'off', 'explain_only'}):
            _fail('configuration_changed')
        if publication is not None and (publication.subscription_version != sub.version
                or publication.destination_id != target.id or publication.destination_version != target.version
                or release.credential_ref != target.credential_ref):
            _fail('configuration_changed')
        return sub, target

    @staticmethod
    def request_id(day, subscription_id):
        return f'formal:{day}:{subscription_id}'

    def _existing(self, session, day, subscription_id, batch_id):
        row = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.request_id == self.request_id(day, subscription_id)))
        if row is None:
            return None
        if row.kind != 'formal':
            _fail('idempotency_kind_conflict')
        publication = session.get(ResearchPublication, row.publication_id)
        release = session.get(PublicationRelease, row.publication_id)
        if publication is None or release is None or release.batch_id != batch_id or publication.subscription_id != subscription_id:
            _fail('daily_binding_conflict')
        return publication, release, row

    def prepare(self, subscription_id, batch_id, *, lease_token):
        # Date+subscription identity is consulted before any LLM, even on reentry.
        with self.repository.atomic() as session:
            batch = session.get(ResearchBatch, batch_id)
            if batch is None:
                _fail('batch_not_found')
            existing = self._existing(session, batch.business_date, subscription_id, batch_id)
            if existing is not None:
                pub, release, outbox = existing
                if outbox.status not in TERMINAL:
                    self._control(session, lease_token, release.authorization_version)
                    self._batch(session, batch_id)
                    self._configuration(session, subscription_id, batch, pub, release)
                return ResearchPublisher._publication_public(pub)
            control = self._control(session, lease_token)
            batch, source = self._batch(session, batch_id)
            sub, target = self._configuration(session, subscription_id, batch)
            frozen = dict(epoch=control.version, sub_version=sub.version, target_version=target.version,
                target_id=target.id, credential_ref=target.credential_ref, top_n=sub.top_n, llm_mode=sub.llm_mode,
                batch_hash=batch.input_hash)
            envelope, detail = formal_envelope(source, sub.top_n, format_version=FORMAT_VERSION)
            brief_input = build_brief_input(envelope, detail, source)
        key = content_hash(dict(batch=frozen['batch_hash'], subscription=subscription_id,
            version=frozen['sub_version'], top_n=frozen['top_n'], llm_mode=frozen['llm_mode'],
            format_version=FORMAT_VERSION, prompt_version=PROMPT_VERSION, frozen_input=brief_input))
        llm_input = brief_input
        if not envelope['candidates'] or (not any(row['metrics'] for row in envelope['candidates']) and not envelope['evidence']):
            llm = dict(status='off' if frozen['llm_mode'] == 'off' else 'fallback', reason='no_supported_evidence', explanation=None)
        else:
            llm = self.explanations.explain(llm_input, key, frozen['llm_mode'] == 'explain_only', frozen['llm_mode'])
        if llm.get('explanation') is not None:
            try:
                llm = dict(llm, explanation=validate_brief_explanation(canonical(llm['explanation']), llm_input))
            except (ValueError, TypeError):
                llm = dict(status='fallback', reason='invalid_model_output', explanation=None)
        # ExplanationService already returns this schema; never accidentally
        # make future private telemetry public via a document dictionary merge.
        llm = {key: llm[key] for key in ('status', 'reason', 'model', 'prompt_version', 'usage', 'finish_reason', 'explanation') if key in llm}
        title = next(row['display_name'] for row in self.service.categories() if row['category_id'] == source['category_id'])
        brief = render_brief(title, envelope, detail, llm, brief_input)
        from src.services.research_delivery.brief_rendering import normalize_brief_llm
        llm = normalize_brief_llm(llm, brief)
        rendered, payload = brief['rendered_content'], brief['card_payload']
        if len(canonical(payload).encode('utf-8')) > PAYLOAD_BYTE_LIMIT:
            _fail('payload_too_large')
        document = dict(envelope=envelope, detail=detail, blockers=[], llm=llm,
            title=title, brief_input=brief_input, **brief)
        with self.repository.atomic() as session:
            self._control(session, lease_token, frozen['epoch'])
            batch, source = self._batch(session, batch_id)
            sub, target = self._configuration(session, subscription_id, batch)
            if (batch.input_hash != frozen['batch_hash'] or sub.version != frozen['sub_version']
                    or target.id != frozen['target_id'] or target.version != frozen['target_version']
                    or target.credential_ref != frozen['credential_ref']):
                _fail('configuration_changed')
            existing = self._existing(session, batch.business_date, subscription_id, batch_id)
            if existing is not None:
                pub, release, _ = existing
                if release.authorization_version != frozen['epoch']:
                    _fail('authorization_changed')
                self._configuration(session, subscription_id, batch, pub, release)
                return ResearchPublisher._publication_public(pub)
            if target.keyword and target.keyword not in rendered:
                _fail('destination_keyword_mismatch')
            now = self._utc(self._now())
            pub = ResearchPublication(id=uuid.uuid4().hex, subscription_id=sub.id, destination_id=target.id,
                subscription_version=sub.version, destination_version=target.version, source_run_id=detail['run_id'] or batch.id,
                content_hash=content_hash(document), input_hash=key, source_snapshot_json=batch.result_json,
                document_json=canonical(document), status='prepared', created_at=now)
            release = PublicationRelease(publication_id=pub.id, batch_id=batch.id, authorization_version=frozen['epoch'],
                credential_ref=target.credential_ref, payload_hash=content_hash(payload), payload_json=canonical(payload),
                expires_at=batch.expires_at, created_at=now)
            outbox = DeliveryOutbox(id=uuid.uuid4().hex, request_id=self.request_id(batch.business_date, sub.id), kind='formal',
                destination_id=target.id, destination_version=target.version, credential_ref=target.credential_ref,
                publication_id=pub.id, payload_json=canonical(payload), payload_hash=content_hash(payload),
                status='pending', attempt_count=0, expires_at=release.expires_at, created_at=now)
            job = session.scalar(select(AutomationJob).where(AutomationJob.business_date == batch.business_date,
                AutomationJob.subscription_id == sub.id))
            if job is None:
                job = AutomationJob(id=uuid.uuid4().hex, business_date=batch.business_date, subscription_id=sub.id,
                    category_id=batch.category_id, subscription_version=sub.version, destination_version=target.version,
                    authorization_version=frozen['epoch'], created_at=now)
                session.add(job)
            elif (job.publication_id or job.authorization_version != frozen['epoch']
                    or job.subscription_version != sub.version or job.destination_version != target.version
                    or job.category_id != batch.category_id or job.batch_id not in (None, batch.id)):
                _fail('daily_binding_conflict')
            job.batch_id, job.publication_id, job.delivery_id = batch.id, pub.id, outbox.id
            job.status, job.stage, job.lease_token, job.updated_at = 'prepared', 'prepared', lease_token, now
            session.add_all([pub, release, outbox])
            session.flush()
            return ResearchPublisher._publication_public(pub)

    def deliver(self, publication_id, *, lease_token):
        with self.repository.atomic() as session:
            pub = session.get(ResearchPublication, publication_id)
            release = session.get(PublicationRelease, publication_id)
            row = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.publication_id == publication_id,
                DeliveryOutbox.kind == 'formal'))
            if pub is None or release is None or row is None:
                _fail('formal_publication_not_found')
            now = self._utc(self._now())
            if row.status == 'sending' and row.sending_at and row.sending_at <= now - timedelta(seconds=90):
                row.status, row.error_code, row.completed_at = 'delivery_unknown', 'abandoned_sending', now
            if row.status != 'pending':
                return self.repository.delivery_public(row)
            # A previous owner cannot poison the new owner's pending identity.
            # Lease rejection raises/rolls back; it is NOT a terminal outcome.
            self._lease(session, lease_token)
            try:
                self._control(session, lease_token, release.authorization_version)
                batch, _ = self._batch(session, release.batch_id)
                _, target = self._configuration(session, pub.subscription_id, batch, pub, release)
                if (release.expires_at > batch.expires_at or release.expires_at <= now or row.expires_at != release.expires_at
                        or row.request_id != self.request_id(batch.business_date, pub.subscription_id)
                        or row.destination_id != target.id or row.destination_version != target.version
                        or row.credential_ref != release.credential_ref):
                    _fail('release_binding_changed')
                try:
                    document, payload = json.loads(pub.document_json), json.loads(release.payload_json)
                    if (content_hash(document) != pub.content_hash or content_hash(payload) != release.payload_hash
                            or pub.source_snapshot_json != batch.result_json
                            or row.payload_hash != release.payload_hash or row.payload_json != release.payload_json
                            or payload != frozen_payload(document)
                            or len(canonical(payload).encode('utf-8')) > PAYLOAD_BYTE_LIMIT):
                        _fail('payload_changed')
                except (KeyError, TypeError, ValueError):
                    _fail('payload_changed')
                try:
                    credentials = self.service.vault.resolve(release.credential_ref)
                except CredentialError:
                    _fail('credential_unavailable')
                # Resolving local credentials may take time. Recheck before the
                # durable claim; network I/O begins only after this transaction.
                self._control(session, lease_token, release.authorization_version)
                self._window(batch.business_date)
                claim_time = self._utc(self._now())
                if batch.expires_at <= claim_time:
                    _fail('batch_expired')
                if release.expires_at <= claim_time:
                    _fail('release_expired')
            except ManagementError as error:
                # Validation/credential resolution can outlast our lease. Do not
                # mutate shared state even when another guard also became invalid.
                self._lease(session, lease_token)
                if error.code == 'lease_lost':
                    raise
                row.status, row.error_code, row.completed_at = 'suppressed', error.code, now
                self._job_state(session, row, lease_token, now)
                return self.repository.delivery_public(row)
            row.status, row.sending_at, row.attempt_count = 'sending', claim_time, 1
            self._job_state(session, row, lease_token, claim_time)
            delivery_id = row.id
        try:
            outcome = self.sender(credentials, payload)
            if not isinstance(outcome, DeliveryOutcome) or outcome.status not in {'sent', 'rejected', 'delivery_unknown'}:
                outcome = DeliveryOutcome('delivery_unknown', 'invalid_transport_outcome')
        except Exception:
            outcome = DeliveryOutcome('delivery_unknown', 'transport_exception')
        with self.repository.atomic() as session:
            row = session.get(DeliveryOutbox, delivery_id)
            if row.status == 'sending':
                row.status, row.error_code, row.completed_at = outcome.status, outcome.error_code, self._utc(self._now())
                self._job_state(session, row, lease_token, row.completed_at)
            return self.repository.delivery_public(row)

    @staticmethod
    def _job_state(session, outbox, lease_token, now):
        job = session.scalar(select(AutomationJob).where(AutomationJob.delivery_id == outbox.id))
        if job:
            job.status, job.stage, job.error_code = outbox.status, 'delivery', outbox.error_code
            job.lease_token, job.updated_at = lease_token, now
