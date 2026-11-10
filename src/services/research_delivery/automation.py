"""App-owned daily research worker. SQLite is the durable coordinator.

One execution thread performs bounded ticks; a separate heartbeat renews its
lease during slow providers/models. Revoking a lease fences future commits, not
an already committed transport call. No request starts collection or delivery.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from copy import deepcopy
from dataclasses import replace
from contextlib import contextmanager
import json
import math
import re
import threading
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select

from src.repositories.automation_models import AutomationControl, AutomationJob, AutomationRunRequest, ResearchBatch, AutomationProgress
from src.repositories.research_delivery_models import DeliveryDestination, DeliveryOutbox, DeliverySubscription
from src.repositories.research_delivery_repository import ManagementError
from src.services.research_delivery.formal_publisher import FormalPublisher, AUXILIARY, TERMINAL
from src.services.research_delivery.live_contracts import ProducerResult
from src.services.research_delivery.timing import (
    PUBLICATION_WINDOW_END,
    PUBLICATION_WINDOW_END_TIME,
    PUBLICATION_WINDOW_START,
    PUBLICATION_WINDOW_START_TIME,
)

SHANGHAI = ZoneInfo('Asia/Shanghai')
FINAL = TERMINAL | {'blocked', 'skipped'}

# 投递顺序（一个一个来、完成即推）。
#
# 为什么需要显式顺序：所有群现在共用同一个放行时间（窗口起点），`due` 不再区分先后，
# 若不加约束，出队顺序就取决于数据库返回顺序（不可预期、易随重建变化）。
# 顺序依据：短线策略（量价收盘即定、次日开盘要用）→ 大盘 → 中长线因子/价值 →
# ETF → 公告（16:00 后才陆续披露，必须末位）。
# 不在表内的类别排在最后（保持稳定，不报错）。
DELIVERY_ORDER = (
    'volume_breakout',
    'capital_heat',
    'oversold_reversal',
    'shrink_pullback',
    'market_radar',
    'momentum_quality',
    'balanced_alpha',
    'dual_low',
    'quality_value',
    'low_volatility_quality',
    'blue_chip_income',
    'etf_rotation',
    'announcement_risk',
)


def _delivery_rank(category_id):
    try:
        return DELIVERY_ORDER.index(category_id)
    except ValueError:
        return len(DELIVERY_ORDER)

RETRYABLE = {'source_timeout', 'source_rate_limited', 'source_request_failed',
             'market_response_coverage_below_95pct'}
LEASE_SECONDS = 45
POLL_SECONDS = 5
COVERAGE_KEYS = {'scope', 'independent_universe_count', 'eligible_count', 'required_count', 'valid_count',
    'contract_version', 'evaluation_scope_count', 'evaluated_count', 'pending_count',
    'daily_response_count', 'daily_basic_response_count', 'daily_response_coverage', 'daily_basic_response_coverage',
    'technical_limit', 'technical_planned_count', 'technical_not_evaluated_count', 'unknown_missing_count',
    'snapshot_pass_count', 'pool_count', 'history_years', 'scope_count', 'scanned_count', 'verified_count',
    'unscanned_count', 'failed_count', 'industry_available', 'concept_available', 'asset_count', 'valid_asset_count'}


def _fail(code, status=409):
    raise ManagementError(code, 'Research automation: ' + code, status)


def _codes(values, fallback):
    safe = [v for v in values if isinstance(v, str) and re.fullmatch('[a-z][a-z0-9_]{0,99}', v)]
    return list(dict.fromkeys(safe)) or [fallback]


def _coverage(source):
    result = {}
    for key, value in source.get('coverage', {}).items():
        if key not in COVERAGE_KEYS:
            continue
        if type(value) in (int, float, bool) and (type(value) is bool or math.isfinite(value)):
            result[key] = value
        elif key == 'scope' and value in {'restricted_complete_core_metric_eligible_universe',
                'observed_snapshot_not_full_market', 'verified_theme_subset', 'bounded_public_announcement_metadata'}:
            result[key] = value
    return result


class ResearchAutomation:
    def __init__(self, service, config=None, *, config_provider=None, collector_factory=None,
                 auxiliary=None, publisher_factory=None, clock=None, enricher_factory=None):
        self.service, self.repository = service, service.repository
        if config_provider is not None:
            self.config_provider = config_provider
        elif config is not None:
            self.config_provider = lambda: config
        elif getattr(service, 'config', None) is not None:
            self.config_provider = lambda: service.config
        else:
            from src.config import Config
            self.config_provider = Config  # Explicit management-only injection: no live fallback.
        if collector_factory is None:
            from src.services.research_delivery.live_collector import LiveCollector
            collector_factory = LiveCollector
        if auxiliary is None:
            from src.services.research_delivery.auxiliary_producers import produce_auxiliary
            auxiliary = produce_auxiliary
        self.collector_factory, self.auxiliary = collector_factory, auxiliary
        self.publisher_factory, self.clock = publisher_factory or FormalPublisher, clock
        if enricher_factory is None:
            from src.services.research_delivery.research_context import ResearchContextEnricher
            enricher_factory = ResearchContextEnricher
        self.enricher_factory, self._enricher = enricher_factory, None
        self.owner_id, self._lease_token, self._epoch = uuid.uuid4().hex, None, None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tick_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._thread = self._heartbeat_thread = None
        self._running = False

    def _now(self):
        stamp = self.clock() if self.clock else datetime.now(SHANGHAI)
        if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
            _fail('clock_invalid')
        return stamp.astimezone(SHANGHAI)

    @staticmethod
    def _utc(stamp):
        return stamp.astimezone(timezone.utc).replace(tzinfo=None)

    def _control(self, session):
        row = session.get(AutomationControl, 'main')
        if row is None:
            row = AutomationControl(id='main', enabled=False, version=1, updated_at=self._utc(self._now()))
            session.add(row)
            session.flush()
        return row

    def _guard(self, session, *, enabled=True):
        row = session.get(AutomationControl, 'main')
        now = self._utc(self._now())
        if (self._stop.is_set() or row is None or not self._lease_token or row.owner_id != self.owner_id
                or row.lease_token != self._lease_token or row.lease_until is None or row.lease_until <= now):
            _fail('lease_lost')
        if row.version != self._epoch:
            _fail('authorization_changed')
        if enabled and not row.enabled:
            _fail('automation_paused')
        return row

    def _claim(self):
        if self._stop.is_set():
            return False
        with self.repository.atomic() as session:
            control = self._control(session)
            now = self._utc(self._now())
            if control.lease_until and control.lease_until > now:
                if control.owner_id != self.owner_id or control.lease_token != self._lease_token:
                    return False
            else:
                control.owner_id, control.lease_token = self.owner_id, uuid.uuid4().hex
            self._lease_token, self._epoch = control.lease_token, control.version
            control.lease_until, control.heartbeat_at = now + timedelta(seconds=LEASE_SECONDS), now
            return True

    def _heartbeat(self):
        if self._lease_token is None:
            return
        with self.repository.atomic() as session:
            control = self._guard(session, enabled=False)
            now = self._utc(self._now())
            control.heartbeat_at, control.lease_until = now, now + timedelta(seconds=LEASE_SECONDS)

    def start(self):
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return
            if self._running:
                _fail('worker_stopping')
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name='research-automation', daemon=True)
            self._heartbeat_thread = threading.Thread(target=self._pulse, name='research-heartbeat', daemon=True)
            self._thread.start()
            self._heartbeat_thread.start()

    def _loop(self):
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def _pulse(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._heartbeat()
            except ManagementError:
                # Do not reacquire under an in-flight tick. Its token is fenced.
                pass

    def stop(self):
        self._stop.set()
        self._wake.set()
        # Revoke before joining: a slow provider may ignore cancellation, but
        # its old token can never commit prepare/sending after this transaction.
        with self.repository.atomic() as session:
            control = session.get(AutomationControl, 'main')
            if control and control.owner_id == self.owner_id and control.lease_token == self._lease_token:
                control.owner_id = control.lease_token = control.lease_until = None
        for thread in (self._heartbeat_thread, self._thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)

    def update(self, version, enabled, subscription_ids):
        if (type(version) is not int or version < 1 or type(enabled) is not bool
                or not isinstance(subscription_ids, list) or any(not isinstance(v, str) for v in subscription_ids)
                or len(subscription_ids) != len(set(subscription_ids))):
            _fail('invalid_request', 422)
        categories = {r['category_id'] for r in self.service.categories()}
        with self.repository.atomic() as session:
            control = self._control(session)
            if control.version != version:
                _fail('version_conflict')
            selected = []
            for record_id in subscription_ids:
                sub = session.get(DeliverySubscription, record_id)
                target = session.get(DeliveryDestination, sub.destination_id) if sub else None
                if sub is None:
                    _fail('unknown_subscription', 422)
                if (sub.strategy_id not in categories or target is None or not target.enabled or not target.credential_ref
                        or not PUBLICATION_WINDOW_START <= sub.planned_time < PUBLICATION_WINDOW_END):
                    _fail('subscription_not_configured', 422)
                selected.append(sub)
            now = self._utc(self._now())
            # Only a global enabled-state transition revokes existing authority.
            # Additive activation and redundant enables commute within the same
            # epoch, preserving unrelated jobs, releases and the current lease.
            if control.enabled != enabled:
                for row in session.scalars(select(AutomationJob).where(AutomationJob.status.not_in(FINAL))):
                    if row.status != 'sending':
                        row.status, row.stage, row.error_code, row.updated_at = 'suppressed', 'authorization', 'authorization_changed', now
                for row in session.scalars(select(DeliveryOutbox).where(DeliveryOutbox.kind == 'formal', DeliveryOutbox.status == 'pending')):
                    row.status, row.error_code, row.completed_at = 'suppressed', 'authorization_changed', now
                control.enabled, control.version, control.updated_at = enabled, version + 1, now
                control.owner_id = control.lease_token = control.lease_until = None
                control.last_error = None
            for sub in selected:
                if not sub.enabled:
                    sub.enabled, sub.version, sub.updated_at = True, sub.version + 1, now
        self._wake.set()
        return self.status()

    def _ensure_jobs(self, session, control, force=False):
        now, day = self._utc(self._now()), self._now().date().isoformat()
        jobs = []
        for sub in session.scalars(select(DeliverySubscription).where(DeliverySubscription.enabled.is_(True))):
            target = session.get(DeliveryDestination, sub.destination_id)
            row = session.scalar(select(AutomationJob).where(AutomationJob.business_date == day, AutomationJob.subscription_id == sub.id))
            if row is None:
                row = AutomationJob(id=uuid.uuid4().hex, business_date=day, subscription_id=sub.id,
                    category_id=sub.strategy_id, subscription_version=sub.version,
                    destination_version=target.version if target else 0, authorization_version=control.version,
                    status='pending', stage='queued', attempts=0, force_requested=force, created_at=now, updated_at=now)
                session.add(row)
            elif force and row.status not in FINAL and row.authorization_version == control.version:
                row.force_requested = True
            jobs.append(row)
        session.flush()
        # 出队顺序按内容特点固定：所有群共用同一个放行时间时，顺序不能交给数据库决定。
        jobs.sort(key=lambda job: (_delivery_rank(job.category_id), job.category_id or ''))
        return jobs

    def request_run(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,100}', request_id):
            _fail('invalid_request', 422)
        day = self._now().date().isoformat()
        with self.repository.atomic() as session:
            control = self._control(session)
            receipt = session.get(AutomationRunRequest, request_id)
            if receipt:
                if receipt.business_date != day:
                    _fail('request_id_day_conflict')
            else:
                if not control.enabled:
                    _fail('automation_paused')
                receipt = AutomationRunRequest(request_id=request_id, business_date=day,
                    authorization_version=control.version, created_at=self._utc(self._now()))
                session.add(receipt)
                self._ensure_jobs(session, control, force=True)
            result = dict(request_id=receipt.request_id, business_date=receipt.business_date,
                authorization_version=receipt.authorization_version, status='accepted')
        self._wake.set()
        return result

    def status(self):
        now = self._utc(self._now())
        with self.repository.session_factory() as session:
            control = session.get(AutomationControl, 'main')
            alive = bool(control and control.heartbeat_at and control.heartbeat_at > now - timedelta(seconds=LEASE_SECONDS)
                and control.lease_token and control.lease_until and control.lease_until > now)
            running = False
            jobs = {}
            for row in session.scalars(select(AutomationJob).order_by(AutomationJob.business_date.desc(), AutomationJob.created_at.desc())):
                if row.subscription_id not in jobs:
                    jobs[row.subscription_id] = row
            items = []
            for sub in session.scalars(select(DeliverySubscription).order_by(DeliverySubscription.created_at, DeliverySubscription.id)):
                row = jobs.get(sub.id)
                public = None
                if row:
                    public = {key: getattr(row, key) for key in ('id', 'business_date', 'status', 'stage', 'attempts',
                        'error_code', 'batch_id', 'publication_id', 'delivery_id', 'updated_at')}
                    outbox = session.get(DeliveryOutbox, row.delivery_id) if row.delivery_id else None
                    # Public effective state is read-only: terminal transport or
                    # 90-second sending ambiguity remains truthful after pause,
                    # cutoff, next day, or an old epoch without granting writes.
                    if outbox and outbox.status in TERMINAL:
                        public.update(status=outbox.status, stage='delivery', error_code=outbox.error_code,
                            updated_at=outbox.completed_at or row.updated_at)
                    elif (outbox and outbox.status == 'sending' and outbox.sending_at
                            and outbox.sending_at <= now - timedelta(seconds=90)):
                        public.update(status='delivery_unknown', stage='delivery', error_code='abandoned_sending',
                            updated_at=outbox.sending_at + timedelta(seconds=90))
                    running = running or bool(alive and control.enabled and row.authorization_version == control.version
                        and row.lease_token == control.lease_token and row.business_date == self._now().date().isoformat()
                        and public['status'] in {'collecting', 'preparing', 'sending'})
                    batch = session.get(ResearchBatch, row.batch_id) if row.batch_id else None
                    progress = session.get(AutomationProgress, row.id) if not batch else None
                    public['coverage'] = (_coverage(json.loads(batch.result_json)) if batch else
                        _coverage({'coverage': json.loads(progress.coverage_json)}) if progress else {})
                items.append(dict(subscription_id=sub.id, name=sub.name, category_id=sub.strategy_id,
                    destination_id=sub.destination_id, planned_time=sub.planned_time, enabled=sub.enabled, job=public))
            return dict(enabled=bool(control and control.enabled), version=control.version if control else 1,
                worker_alive=alive, heartbeat_at=control.heartbeat_at if control else None,
                timezone='Asia/Shanghai', collection_time=PUBLICATION_WINDOW_START,
                cutoff_time=PUBLICATION_WINDOW_END,
                running=running, subscriptions=items, last_error=control.last_error if control else None)

    def _job_guard(self, session, row):
        self._guard(session)
        if row.authorization_version != self._epoch:
            _fail('authorization_changed')
        sub = session.get(DeliverySubscription, row.subscription_id)
        target = session.get(DeliveryDestination, sub.destination_id) if sub else None
        if (not sub or not sub.enabled or not target or not target.enabled or not target.credential_ref
                or sub.version != row.subscription_version or target.version != row.destination_version
                or sub.strategy_id != row.category_id):
            _fail('configuration_changed')
        return sub

    def _write(self, job_id, **values):
        with self.repository.atomic() as session:
            row = session.get(AutomationJob, job_id)
            self._job_guard(session, row)
            if 'progress_coverage' in values:
                coverage = _coverage({'coverage': values.pop('progress_coverage')})
                progress = session.get(AutomationProgress, job_id)
                if progress is None:
                    progress = AutomationProgress(job_id=job_id)
                    session.add(progress)
                progress.coverage_json = json.dumps(coverage, sort_keys=True, allow_nan=False)
                progress.updated_at = self._utc(self._now())
            for key, value in values.items():
                setattr(row, key, value)
            row.lease_token, row.updated_at = self._lease_token, self._utc(self._now())

    def _progress(self, *args, **kwargs):
        with self.repository.session_factory() as session:
            self._guard(session)
        if not PUBLICATION_WINDOW_START_TIME <= self._now().time() < PUBLICATION_WINDOW_END_TIME:
            _fail('outside_publication_window')

    @contextmanager
    def _checkpoint_guard(self):
        # Local file publication only: never hold this transaction over source
        # requests. _claim uses the same BEGIN IMMEDIATE coordinator.
        with self.repository.atomic() as session:
            self._guard(session)
            if not PUBLICATION_WINDOW_START_TIME <= self._now().time() < PUBLICATION_WINDOW_END_TIME:
                _fail('outside_publication_window')
            yield

    def _batch_cache(self, day, versions):
        with self.repository.session_factory() as session:
            rows = session.scalars(select(ResearchBatch).where(ResearchBatch.business_date == day,
                ResearchBatch.expires_at > self._utc(self._now())).order_by(ResearchBatch.created_at.desc()))
            result = {}
            for row in rows:
                if versions.get(row.category_id) == row.strategy_version and row.category_id not in result:
                    result[row.category_id] = (row.id, json.loads(row.result_json))
            return result

    def _research_history(self, day):
        """Same-day recovery is independent of publication version/expiry gates."""
        with self.repository.session_factory() as session:
            rows = session.scalars(select(ResearchBatch).where(ResearchBatch.business_date == day)
                .order_by(ResearchBatch.created_at))
            return [json.loads(row.result_json) for row in rows]

    def _producer_result(self, publisher, row, result, cache):
        self._progress()
        if not isinstance(result, ProducerResult) or result.category_id != row['category_id'] or result.business_date != row['business_date']:
            _fail('producer_contract_invalid')
        if result.state in {'ready', 'degraded'} and not result.blockers:
            saved = publisher.store_batch(result)
            self._write(row['id'], batch_id=saved['id'], status='ready', stage='collected', error_code=None, retry_at=None)
            cache[result.category_id] = (saved['id'], result.to_dict())
        else:
            codes = _codes(result.blockers if result.state != 'skipped' else result.warnings, 'producer_' + result.state)
            coverage = result.coverage
            if (codes == ['collection_pending'] and coverage.get('contract_version') == 2
                    and coverage.get('failed_count') == 0 and coverage.get('pending_count', 0) > 0
                    and coverage.get('evaluated_count', -1) + coverage['pending_count'] == coverage.get('evaluation_scope_count')):
                # Work-budget exhaustion is progress, not a source retry. The
                # current attempt was provisionally incremented at dispatch.
                row['attempts'] = max(0, row['attempts'] - 1)
                self._write(row['id'], status='retry_wait', stage='collection_pending',
                    error_code='collection_pending', attempts=row['attempts'],
                    progress_coverage=coverage,
                    retry_at=self._utc(self._now()) + timedelta(seconds=POLL_SECONDS))
                return
            retry = result.state != 'skipped' and any(code in RETRYABLE for code in codes) and not any(
                code in {'source_permission_denied', 'source_quota_denied'} or re.search('identity|date|conflict|invalid|truncated', code)
                for code in codes) and row['attempts'] < 3
            self._write(row['id'], status='retry_wait' if retry else ('skipped' if result.state == 'skipped' else 'blocked'),
                stage='collection', error_code=';'.join(codes),
                progress_coverage=coverage,
                retry_at=self._utc(self._now()) + timedelta(minutes=10) if retry else None)

    def _enriched_result(self, result, related, top_n, config, evidence_dir, recovery=()):
        if not isinstance(result, ProducerResult) or result.state not in {'ready', 'degraded'} or result.blockers:
            return result
        self._progress()
        interrupted = []
        def progress(*args, **kwargs):
            try:
                self._progress(*args, **kwargs)
            except Exception as error:
                interrupted.append(error)
                raise
        try:
            if self._enricher is None:
                self._enricher = self.enricher_factory(config, evidence_dir=evidence_dir, clock=self._now)
            elif hasattr(self._enricher, 'configure'):
                self._enricher.configure(config)
            else:
                self._enricher.config = config
            if hasattr(self._enricher, 'restore'):
                self._enricher.restore(recovery, business_date=result.business_date, progress=progress)
            enriched = self._enricher.enrich(result, related_results=related,
                top_n_by_strategy=top_n, progress=progress)
        except ManagementError:
            raise
        except Exception:
            if interrupted:
                raise interrupted[0]
            # Optional facts never block the base immutable batch. No exception
            # text/config is persisted, and no recollection/republication occurs.
            from src.services.research_delivery.market_context import empty_market_context
            payload = deepcopy(result.result)
            context = payload.get('research_context')
            if not isinstance(context, dict):
                context = empty_market_context(datetime.fromisoformat(result.business_date).date())
            context.setdefault('statuses', {})['background'] = 'enrichment_failed'
            payload['research_context'] = context
            enriched = replace(result, result=payload)
        if interrupted:
            raise interrupted[0]
        self._progress()
        return enriched

    def _error(self, job_id, error):
        if error.code in {'lease_lost', 'authorization_changed', 'automation_paused'}:
            raise error
        # Configuration mismatch may itself prevent the normal checkpoint. It is
        # still safe for the current lease/epoch to suppress that stale job.
        with self.repository.atomic() as session:
            self._guard(session)
            row = session.get(AutomationJob, job_id)
            if row.authorization_version != self._epoch:
                _fail('authorization_changed')
            row.status, row.stage, row.error_code = 'suppressed' if error.code == 'configuration_changed' else 'blocked', 'guard', error.code
            row.lease_token, row.updated_at = self._lease_token, self._utc(self._now())

    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            if not self._claim():
                return
            with self.repository.session_factory() as session:
                if not self._guard(session, enabled=False).enabled:
                    return
            if self._now().time() >= PUBLICATION_WINDOW_END_TIME:
                self._expire()
                return
            if self._now().time() < PUBLICATION_WINDOW_START_TIME:
                return
            self._running = True
            self._execute()
        except ManagementError as error:
            if error.code not in {'lease_lost', 'authorization_changed', 'automation_paused'}:
                self._last_error(error.code)
        except Exception:
            self._last_error('worker_execution_failed')
        finally:
            self._running = False
            self._tick_lock.release()

    def _last_error(self, code):
        try:
            with self.repository.atomic() as session:
                self._guard(session)
                session.get(AutomationControl, 'main').last_error = code
        except ManagementError:
            pass

    def _expire(self):
        with self.repository.atomic() as session:
            self._guard(session)
            now = self._utc(self._now())
            for row in session.scalars(select(AutomationJob).where(AutomationJob.business_date == self._now().date().isoformat(),
                    AutomationJob.authorization_version == self._epoch, AutomationJob.status.not_in(FINAL | {'sending'}))):
                row.status, row.stage, row.error_code = 'suppressed', 'deadline', 'outside_publication_window'
                row.lease_token, row.updated_at = self._lease_token, now
                outbox = session.get(DeliveryOutbox, row.delivery_id) if row.delivery_id else None
                if outbox and outbox.status == 'pending':
                    outbox.status, outbox.error_code, outbox.completed_at = 'suppressed', 'outside_publication_window', now

    def _execute(self):
        day = self._now().date().isoformat()
        with self.repository.atomic() as session:
            control = self._guard(session)
            rows = self._ensure_jobs(session, control)
            work = []
            for row in rows:
                if row.status in FINAL:
                    continue
                self._guard(session)
                if row.authorization_version != self._epoch:
                    # Old daily identities are not rebound, even while merely
                    # aligning metadata from an independently suppressed outbox.
                    continue
                # Sync repository-suppressed outbox without selecting a new batch.
                outbox = session.get(DeliveryOutbox, row.delivery_id) if row.delivery_id else None
                if outbox and outbox.status in TERMINAL:
                    row.status, row.stage, row.error_code, row.updated_at = outbox.status, 'delivery', outbox.error_code, self._utc(self._now())
                    row.lease_token = self._lease_token
                    continue
                try:
                    sub = self._job_guard(session, row)
                except ManagementError as error:
                    if error.code in {'lease_lost', 'authorization_changed', 'automation_paused'}:
                        raise
                    row.status, row.error_code, row.stage, row.updated_at = 'suppressed', error.code, 'guard', self._utc(self._now())
                    continue
                if row.retry_at and row.retry_at > self._utc(self._now()):
                    continue
                work.append({key: getattr(row, key) for key in ('id', 'subscription_id', 'category_id', 'business_date', 'batch_id',
                    'publication_id', 'delivery_id', 'attempts', 'force_requested')} | {'planned_time': sub.planned_time})
        if not work:
            return
        config = self.config_provider()
        publisher = self.publisher_factory(self.service, config, clock=self._now)
        versions = {r['strategy_id']: r['version'] for r in self.service.strategies()} | {key: 'auxiliary-v1' for key in AUXILIARY}
        cache = self._batch_cache(day, versions)
        evidence_dir = self.service.vault.root.parent / 'research-delivery-evidence'
        with self.repository.session_factory() as session:
            top_n = {}
            for sub in session.scalars(select(DeliverySubscription).where(DeliverySubscription.enabled.is_(True))):
                top_n[sub.strategy_id] = max(top_n.get(sub.strategy_id, 0), sub.top_n)
        stocks = [r for r in work if r['category_id'] not in AUXILIARY and r['category_id'] in versions and not r['batch_id']]
        pending = [r for r in stocks if r['category_id'] not in cache]
        collector = None
        if pending:
            collector = self.collector_factory(config, evidence_dir, now=self._now)
            from src.services.research_delivery.live_collector import LiveCollector
            if isinstance(collector, LiveCollector):
                collector.checkpoint_guard = self._checkpoint_guard
            for row in pending:
                row['attempts'] += 1
                self._write(row['id'], status='collecting', stage='collecting', attempts=row['attempts'])
            self._progress()
            try:
                results = collector.collect(sorted({r['category_id'] for r in pending}), day, progress=self._progress)
            except ManagementError:
                raise
            except Exception as error:
                from src.services.research_delivery.live_collector import CollectionError
                code = _codes([str(error)] if isinstance(error, CollectionError) else [], 'collection_failed')[0]
                results = {r['category_id']: ProducerResult(r['category_id'], day, 'blocked', blockers=[code]) for r in pending}
            self._progress()
            # Every completed collection is visible before the first enrichment;
            # loop order cannot hide another current strategy's cross-signal.
            related = [v[1] for v in cache.values()] + list(results.values())
            recovery = self._research_history(day)
            enriched_results = {}
            for row in pending:
                try:
                    category = row['category_id']
                    if category not in enriched_results:
                        enriched_results[category] = self._enriched_result(results.get(category), related, top_n, config, evidence_dir, recovery)
                    self._producer_result(publisher, row, enriched_results[category], cache)
                except ManagementError as error:
                    self._error(row['id'], error)
        # Auxiliary-only configurations still require actual exchange calendar
        # evidence. Calendar failures are never guessed to be closed/open.
        for row in work:
            try:
                self._progress()
                with self.repository.session_factory() as session:
                    current = session.get(AutomationJob, row['id'])
                    if current.status in FINAL or current.status == 'retry_wait':
                        continue
                    row['batch_id'], row['publication_id'] = current.batch_id, current.publication_id
                due = row['force_requested'] or self._now().strftime('%H:%M') >= row['planned_time']
                category = row['category_id']
                if not row['batch_id'] and category in cache:
                    row['batch_id'] = cache[category][0]
                    self._write(row['id'], batch_id=row['batch_id'], status='ready', stage='collected', error_code=None)
                if not row['batch_id'] and category in AUXILIARY and due:
                    row['attempts'] += 1
                    self._write(row['id'], attempts=row['attempts'], status='collecting', stage='collecting')
                    calendars = [source.get('coverage', {}).get('calendar') for _, source in cache.values()]
                    if not any(c == dict(exchange='SSE', business_date=day, is_open=1) for c in calendars):
                        collector = collector or self.collector_factory(config, evidence_dir, now=self._now)
                        calendar = collector.trading_day(day, progress=self._progress)
                        if not isinstance(calendar, dict) or type(calendar.get('is_open')) is not int or calendar['is_open'] not in (0, 1):
                            _fail('calendar_evidence_invalid')
                        if not calendar['is_open']:
                            self._write(row['id'], status='skipped', stage='collection', error_code='non_trading_day')
                            continue
                    result = self.auxiliary(category, business_date=day, config=config, evidence_dir=evidence_dir,
                        collector_context={'stock_batches': [v[1] for v in cache.values() if v[1]['category_id'] not in AUXILIARY]},
                        now=self._now, progress=self._progress)
                    self._producer_result(publisher, row, result, cache)
                    row['batch_id'] = cache.get(category, (None,))[0]
                if category not in versions:
                    self._write(row['id'], status='blocked', stage='collection', error_code='category_unsupported')
                    continue
                if not due or not row['batch_id']:
                    continue
                if not row['publication_id']:
                    self._write(row['id'], status='preparing', stage='preparing')
                    publication = publisher.prepare(row['subscription_id'],
                        row['batch_id'], lease_token=self._lease_token)
                    row['publication_id'] = publication['id']
                self._progress()
                publisher.deliver(row['publication_id'], lease_token=self._lease_token)
            except ManagementError as error:
                self._error(row['id'], error)
            except Exception as error:
                from src.services.research_delivery.live_collector import CollectionError
                code = str(error) if isinstance(error, CollectionError) else 'category_execution_failed'
                code = _codes([code], 'category_execution_failed')[0]
                self._write(row['id'], status='retry_wait' if code in RETRYABLE and row['attempts'] < 3 else 'blocked',
                    stage='collection', error_code=code, retry_at=self._utc(self._now()) + timedelta(minutes=10) if code in RETRYABLE else None)
