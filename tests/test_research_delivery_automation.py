"""Offline native automation: actual SQLite/publisher, fake clocks and providers."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from tests.test_research_delivery_formal import formal
from tests.test_research_delivery_aggregation import research
from src.notification_sender.feishu_sender import DeliveryOutcome
from src.repositories.automation_models import AutomationControl, AutomationJob, AutomationRunRequest, ResearchBatch
from src.repositories.research_delivery_models import DeliveryOutbox, DeliverySubscription
from src.repositories.research_delivery_repository import ManagementError
from src.services.research_delivery.automation import ResearchAutomation
from src.services.research_delivery.evidence_store import EvidenceStore
from src.services.research_delivery.research_context import ResearchContextEnricher


@pytest.fixture
def runtime(formal, monkeypatch, tmp_path):
    publisher, sub, source, clock = formal
    source = replace(source, evidence=[EvidenceStore(tmp_path / 'early-evidence').capture_payload(
        # 证据观察时间取窗口起点（15:30）：早于该时间的时钟会被就绪门禁判为
        # 「证据来自未来」而 block，因此这里必须随 timing 的窗口起点一起前移。
        'test', 'snapshot', {'public': True}, fetched_at=clock[0].replace(hour=15, minute=30),
        business_date=clock[0].date(), provenance='provider_normalized')])
    def forbidden(*a, **k):
        pytest.fail('live configuration/database/network forbidden')
    import src.config
    import api.deps
    from src.storage import DatabaseManager
    monkeypatch.setattr(src.config, 'get_config', forbidden)
    monkeypatch.setattr(api.deps, 'get_config', forbidden)
    monkeypatch.setattr(DatabaseManager, 'get_instance', forbidden)
    with publisher.repository.atomic() as session:
        session.query(AutomationControl).delete()
        session.get(DeliverySubscription, sub['id']).enabled = False
    calls, sends = [], []
    publisher.sender = lambda *a: sends.append(a) or DeliveryOutcome('sent', None)
    class Collector:
        def __init__(self, *a, **k): pass
        def collect(self, ids, day, progress=None):
            calls.append(list(ids))
            if progress: progress({'stage': 'collecting'})
            return {key: replace(source, category_id=key, business_date=day) for key in ids}
        def trading_day(self, day, progress=None): return {'is_open': 1}
    worker = ResearchAutomation(publisher.service, publisher.config, collector_factory=Collector,
        publisher_factory=lambda service, config, **kw: publisher, clock=lambda: clock[0],
        enricher_factory=lambda config, **kw: ResearchContextEnricher(config, **kw,
            fundamental_fetcher=Mock(return_value={}),
            news_fetcher=Mock(return_value=SimpleNamespace(success=True, results=[])),
            announcement_fetcher=Mock(return_value=dict(ok=True, records=[], fetched_at=clock[0].isoformat()))))
    yield worker, publisher, sub, source, clock, calls, sends
    worker.stop()


def activate(worker, sub):
    return worker.update(1, True, [sub['id']])


def test_pending_batches_do_not_consume_source_retry_budget_or_send(runtime):
    worker, _, sub, source, clock, calls, sends = runtime
    activate(worker, sub)
    class Pending:
        def __init__(self, *args, **kwargs): pass
        def collect(self, ids, day, progress=None):
            calls.append(ids)
            return {key: replace(source, category_id=key, state='blocked', result={}, blockers=['collection_pending'],
                coverage=dict(contract_version=2, evaluation_scope_count=600, evaluated_count=100 * len(calls),
                              pending_count=600 - 100 * len(calls), failed_count=0)) for key in ids}
    worker.collector_factory = Pending
    for _ in range(5):
        worker.tick()
        assert job(worker)['status'] == 'retry_wait'
        assert job(worker)['stage'] == 'collection_pending' and job(worker)['attempts'] == 0
        assert job(worker)['coverage'] == dict(contract_version=2, evaluation_scope_count=600,
            evaluated_count=100 * len(calls), pending_count=600 - 100 * len(calls), failed_count=0)
        clock[0] += timedelta(seconds=6)
    assert len(calls) == 5 and not sends
    restarted = ResearchAutomation(worker.service, worker.config_provider(), clock=lambda: clock[0])
    assert job(restarted)['coverage'] == job(worker)['coverage']
    with worker.repository.session_factory() as session:
        assert session.query(ResearchBatch).count() == 0


def test_checkpoint_write_is_fenced_after_real_worker_lease_handoff(runtime, tmp_path):
    from src.services.research_delivery.collection_checkpoint import CollectionCheckpoint
    from tests.test_research_collection_checkpoint import state
    worker, publisher, sub, _, clock, _, sends = runtime
    activate(worker, sub)
    assert worker._claim()
    directory = tmp_path / 'checkpoint-fence'
    identity = state(directory)
    checkpoint = CollectionCheckpoint(EvidenceStore(directory), clock[0].date(), identity, guard=worker._checkpoint_guard)
    saved = checkpoint.load(clock[0])
    before = checkpoint.pointer.read_bytes()
    clock[0] += timedelta(seconds=46)
    takeover = ResearchAutomation(worker.service, publisher.config, clock=lambda: clock[0])
    assert takeover._claim()
    # New lease owner need not have written a checkpoint yet: the database
    # publication guard must reject the old writer at the commit boundary.
    with pytest.raises(ManagementError) as error:
        checkpoint.save(saved, clock[0])
    assert error.value.code == 'lease_lost'
    assert checkpoint.pointer.read_bytes() == before and not sends
    current = CollectionCheckpoint(EvidenceStore(directory), clock[0].date(), identity, guard=takeover._checkpoint_guard)
    current.save(current.load(clock[0]), clock[0])


def test_real_collector_pending_coverage_survives_worker_restart(runtime):
    from tests.test_research_delivery_live_collector import LiveCollector
    from tests.test_research_delivery_live_collector import Client
    worker, publisher, sub, _, clock, _, sends = runtime
    activate(worker, sub)
    collectors = []
    def factory(config, directory, **kwargs):
        collector = LiveCollector(config, directory, client=Client(3), request_interval=0, batch_size=1, **kwargs)
        collectors.append(collector)
        return collector
    worker.collector_factory = factory
    worker.tick()
    actual = job(worker)
    assert actual['status'] == 'retry_wait' and actual['stage'] == 'collection_pending'
    assert actual['attempts'] == 0 and actual['batch_id'] is None and not sends
    assert {key: actual['coverage'][key] for key in ('evaluation_scope_count', 'evaluated_count', 'pending_count', 'failed_count')} == dict(
        evaluation_scope_count=3, evaluated_count=1, pending_count=2, failed_count=0)
    assert collectors[0].checkpoint_guard.__self__ is worker
    restarted = ResearchAutomation(worker.service, publisher.config, clock=lambda: clock[0])
    assert job(restarted)['coverage'] == actual['coverage']
    with worker.repository.session_factory() as session:
        assert session.query(ResearchBatch).count() == 0


def job(worker):
    return worker.status()['subscriptions'][0]['job']


def test_disabled_default_and_atomic_selection_validation(runtime):
    worker, publisher, sub, _, _, calls, sends = runtime
    assert worker.status()['enabled'] is False
    assert worker.status()['worker_alive'] is False
    worker.tick()
    assert not calls and not sends
    for ids in ([sub['id'], sub['id']], [sub['id'], 'unknown']):
        with pytest.raises(ManagementError): worker.update(1, True, ids)
        assert worker.status()['version'] == 1
        assert not publisher.repository.get(DeliverySubscription, sub['id'])['enabled']
    activate(worker, sub)
    with pytest.raises(ManagementError): worker.update(1, False, [])


def test_planned_time_activation_window_starts_at_1530(runtime):
    """计划时间必须落在发布时窗内才可启用（时窗起点已从 17:05 前移到 15:00）。

    起点是硬校验边界：把起点改回窗口内更晚的时间会让 15:10 这类时间启不动，
    因此这里同时锁定「窗口内最早可启用时间」和「窗口外仍被拒绝」。
    """
    worker, publisher, sub, _, _, _, _ = runtime

    outside = publisher.service.update_subscription(
        sub['id'], {'version': sub['version'], 'planned_time': '15:29'})
    with pytest.raises(ManagementError):
        worker.update(1, True, [outside['id']])
    assert not publisher.repository.get(DeliverySubscription, sub['id'])['enabled']

    inside = publisher.service.update_subscription(
        sub['id'], {'version': outside['version'], 'planned_time': '15:30'})
    assert worker.update(1, True, [inside['id']])['enabled'] is True
    assert publisher.repository.get(DeliverySubscription, sub['id'])['enabled']


def test_manual_scheduled_daily_dedupe_and_restart(runtime):
    worker, publisher, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: worker.request_run('manual-request-01'), range(2)))
    worker.tick()
    worker.tick()
    assert job(worker)['status'] == 'sent' and len(calls) == len(sends) == 1
    worker.stop()
    restarted = ResearchAutomation(publisher.service, publisher.config, clock=lambda: clock[0],
        collector_factory=lambda *a, **k: pytest.fail('must retain daily terminal identity'),
        publisher_factory=lambda *a, **k: publisher)
    restarted.tick()
    assert job(restarted)['status'] == 'sent' and len(sends) == 1
    clock[0] += timedelta(days=1)
    with pytest.raises(ManagementError) as error: restarted.request_run('manual-request-01')
    assert error.value.code == 'request_id_day_conflict'
    restarted.stop()


def test_preparation_shared_and_delivery_staged_by_planned_time(runtime):
    worker, publisher, sub, _, clock, calls, sends = runtime
    second = publisher.service.create_subscription(dict(name='second', destination_id=sub['destination_id'],
        strategy_id=sub['strategy_id'], llm_mode='off', planned_time='18:10'))
    worker.update(1, True, [sub['id'], second['id']])
    clock[0] = clock[0].replace(hour=17, minute=5)
    worker.tick()
    assert len(calls) == 1 and not sends
    assert all(s['job']['batch_id'] for s in worker.status()['subscriptions'])
    clock[0] = clock[0].replace(minute=10)
    worker.tick()
    assert len(sends) == 1 and len(calls) == 1
    clock[0] = clock[0].replace(hour=18, minute=10)
    worker.tick()
    assert len(sends) == 2 and len(calls) == 1


@pytest.mark.parametrize('hour,minute', [(15, 29), (22, 30), (0, 1)])
def test_manual_cannot_bypass_window(runtime, hour, minute):
    worker, _, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    worker.request_run('manual-window-01')
    clock[0] = clock[0].replace(hour=hour, minute=minute)
    worker.tick()
    assert not calls and not sends


def test_same_release_time_finishes_without_waiting_for_clock_slots(runtime):
    """所有群共用同一放行时间时：一个 tick 内把全部群跑完推完，不再按整点时段等待。

    这是「15:30 起跑、跑完推送即可、一个一个来」的核心行为：放行只看窗口起点与
    完成情况，不看各群曾经的十分钟间隔。多条订阅共享同一次采集（calls 仍为 1）。
    """
    worker, publisher, sub, _, clock, calls, sends = runtime
    # 复用同一策略与同一群：离线状态下伪造数据源只对该类别有效
    created = [publisher.service.create_subscription(dict(
        name=f'additional-{index}', destination_id=sub['destination_id'],
        strategy_id=sub['strategy_id'], llm_mode='off', planned_time='15:30'))
        for index in range(3)]
    worker.update(1, True, [sub['id']] + [row['id'] for row in created])
    with publisher.repository.atomic() as session:
        # 存量订阅也改用同一个放行时间，模拟「各群统一 15:30」
        session.get(DeliverySubscription, sub['id']).planned_time = '15:30'

    clock[0] = clock[0].replace(hour=15, minute=30)
    worker.tick()

    states = {row['name']: (row['job'] or {}).get('status') for row in worker.status()['subscriptions']}
    errors = {row['name']: (row['job'] or {}).get('error_code') for row in worker.status()['subscriptions']}
    # 共享批次只采集一次，四路订阅在同一轮内依次完成投递
    assert len(calls) == 1
    assert len(sends) == 4, (states, errors)
    assert all(state == 'sent' for state in states.values())


def test_delivery_order_is_explicit_and_stable_for_unknown_categories():
    """出队顺序由内容顺序常量决定，且未知类别排最后而不是抛错。"""
    from src.services.research_delivery.automation import DELIVERY_ORDER, _delivery_rank

    ranks = [_delivery_rank(category) for category in DELIVERY_ORDER]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)
    # 短线策略先于大盘、大盘先于中长线因子、公告类最后
    assert _delivery_rank('volume_breakout') < _delivery_rank('market_radar')
    assert _delivery_rank('market_radar') < _delivery_rank('quality_value')
    assert _delivery_rank('quality_value') < _delivery_rank('announcement_risk')
    assert _delivery_rank('unregistered_category') == len(DELIVERY_ORDER)


def test_low_coverage_bounded_retry_then_current_day_success(runtime):
    worker, _, sub, source, clock, calls, sends = runtime
    activate(worker, sub)
    class Collector:
        def collect(self, ids, day, progress=None):
            calls.append(ids)
            return {key: replace(source, state='blocked', blockers=['market_response_coverage_below_95pct'])
                if len(calls) == 1 else source for key in ids}
    worker.collector_factory = lambda *a, **k: Collector()
    worker.tick()
    assert job(worker)['error_code'] == 'market_response_coverage_below_95pct'
    assert job(worker)['attempts'] == 1 and job(worker)['status'] == 'retry_wait'
    clock[0] += timedelta(minutes=9)
    worker.tick()
    assert len(calls) == 1
    clock[0] += timedelta(minutes=1)
    worker.tick()
    assert job(worker)['status'] == 'sent' and job(worker)['attempts'] == 2
    assert len(sends) == 1


@pytest.mark.parametrize('code,retry', [('source_timeout', True), ('source_rate_limited', True),
    ('source_request_failed', True), ('source_permission_denied', False), ('market_universe_conflict_or_truncated', False),
    ('auxiliary_production_failed', False)])
def test_precise_retry_budget(runtime, code, retry):
    worker, _, sub, source, clock, calls, sends = runtime
    activate(worker, sub)
    class Collector:
        def collect(self, ids, day, progress=None):
            calls.append(ids)
            return {key: replace(source, state='blocked', blockers=[code]) for key in ids}
    worker.collector_factory = lambda *a, **k: Collector()
    for _ in range(5):
        worker.tick()
        clock[0] += timedelta(minutes=10)
    assert len(calls) == (3 if retry else 1)
    assert job(worker)['status'] == 'blocked' and job(worker)['error_code'] == code
    assert not sends


def test_nontrading_day_is_persistent_skipped_not_reset(runtime):
    worker, _, sub, source, clock, calls, sends = runtime
    activate(worker, sub)
    class Collector:
        def collect(self, ids, day, progress=None):
            calls.append(ids)
            return {key: replace(source, state='skipped', warnings=['non_trading_day']) for key in ids}
    worker.collector_factory = lambda *a, **k: Collector()
    worker.tick(); worker.tick()
    assert job(worker)['status'] == 'skipped' and job(worker)['error_code'] == 'non_trading_day'
    worker.request_run('holiday-run-01'); worker.tick()
    assert len(calls) == 1 and not sends


def test_lost_lease_after_collection_has_no_job_writeback(runtime):
    worker, publisher, sub, source, clock, _, sends = runtime
    activate(worker, sub)
    snapshots = []
    class Collector:
        def collect(self, ids, day, progress=None):
            with publisher.repository.atomic() as session:
                control = session.get(AutomationControl, 'main')
                control.lease_token = 'new-owner-token'
                control.owner_id = 'new-owner'
                snapshots.append(session.scalar(select(AutomationJob)).updated_at)
            return {key: source for key in ids}
    worker.collector_factory = lambda *a, **k: Collector()
    worker.tick()
    with publisher.repository.session_factory() as session:
        row = session.scalar(select(AutomationJob))
        assert row.updated_at == snapshots[0] and row.stage == 'collecting'
        assert session.query(ResearchBatch).count() == 0
    assert not sends


@pytest.mark.parametrize('operation', ['pause', 'stop'])
def test_pause_or_stop_while_llm_inflight_fences_publication(runtime, operation):
    worker, publisher, sub, _, _, _, sends = runtime
    activate(worker, sub)
    def explain(*a):
        if operation == 'pause': worker.update(2, False, [])
        else: worker.stop()
        return {'status': 'off', 'reason': 'off', 'explanation': None}
    publisher.explanations.explain = explain
    worker.tick()
    assert not sends
    with publisher.repository.session_factory() as session:
        assert session.query(DeliveryOutbox).count() == 0
    if operation == 'pause':
        worker.update(3, True, [sub['id']]); worker.tick()
        assert job(worker)['status'] == 'suppressed' and not sends


def test_two_workers_one_lease_and_fresh_token_recovery(runtime):
    worker, publisher, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    clock[0] = clock[0].replace(hour=17, minute=5)
    worker.tick()
    old = worker._lease_token
    second = ResearchAutomation(publisher.service, publisher.config, clock=lambda: clock[0],
        collector_factory=lambda *a, **k: pytest.fail('reuse persisted batch'),
        publisher_factory=lambda *a, **k: publisher)
    second.tick()
    assert second._lease_token is None
    clock[0] += timedelta(minutes=10)
    second.tick()
    assert second._lease_token and second._lease_token != old
    assert len(sends) == 1 and len(calls) == 1
    second.stop()


def test_status_public_coverage_whitelist(runtime):
    worker, _, sub, source, _, _, _ = runtime
    activate(worker, sub)
    source.coverage['private_path'] = 'PRIVATE_SENTINEL'
    source.coverage['independent_universe_codes'] = ['PRIVATE_SENTINEL']
    worker.tick()
    state = worker.status()
    assert state['timezone'] == 'Asia/Shanghai' and state['collection_time'] == '15:30'
    assert 'PRIVATE_SENTINEL' not in json.dumps(state, default=str)
    assert state['subscriptions'][0]['job']['coverage']['eligible_count'] == 98


def test_status_etf_saved_coverage_counts_and_scalar_filtering(runtime):
    worker, publisher, sub, _, clock, _, _ = runtime
    coverage = dict(asset_count=8, valid_asset_count=6, history_years=1,
        pool_count=float('inf'), eligible_count=float('nan'), technical_limit=['PRIVATE_SENTINEL'],
        independent_universe_codes=['PRIVATE_SENTINEL'], credentials={'token': 'PRIVATE_SENTINEL'},
        arbitrary_count=99, provenance={'path': 'PRIVATE_SENTINEL'})
    with publisher.repository.atomic() as session:
        session.get(DeliverySubscription, sub['id']).strategy_id = 'etf_rotation'
        session.add(ResearchBatch(id='etf-status-batch', business_date=clock[0].date().isoformat(),
            category_id='etf_rotation', strategy_version='auxiliary-v1', input_hash='0' * 64,
            result_json=json.dumps({'coverage': coverage}), created_at=worker._utc(clock[0]),
            expires_at=worker._utc(clock[0] + timedelta(hours=1))))
        session.add(AutomationJob(id='etf-status-job', business_date=clock[0].date().isoformat(),
            subscription_id=sub['id'], category_id='etf_rotation', subscription_version=sub['version'],
            destination_version=1, authorization_version=1, status='ready', stage='collected',
            batch_id='etf-status-batch', created_at=worker._utc(clock[0]), updated_at=worker._utc(clock[0])))
    state = worker.status()
    assert state['subscriptions'][0]['category_id'] == 'etf_rotation'
    assert state['subscriptions'][0]['job']['coverage'] == dict(asset_count=8, valid_asset_count=6, history_years=1)
    assert 'PRIVATE_SENTINEL' not in json.dumps(state, default=str, allow_nan=False)


def test_cutoff_terminally_suppresses_queued_work(runtime):
    worker, _, sub, _, clock, _, sends = runtime
    activate(worker, sub)
    clock[0] = clock[0].replace(hour=17, minute=5)
    worker.tick()
    clock[0] = clock[0].replace(hour=22, minute=30)
    worker.tick()
    assert job(worker)['status'] == 'suppressed'
    assert job(worker)['error_code'] == 'outside_publication_window' and not sends


def test_collection_exception_does_not_stop_other_category(runtime):
    worker, publisher, sub, _, _, _, sends = runtime
    other = publisher.service.create_subscription(dict(name='radar', destination_id=sub['destination_id'],
        strategy_id='market_radar', llm_mode='off', planned_time='17:10'))
    worker.update(1, True, [sub['id'], other['id']])
    class BrokenCollector:
        def collect(self, *a, **kw): raise RuntimeError('PRIVATE_SENTINEL')
        def trading_day(self, *a, **kw): return {'is_open': 1}
    worker.collector_factory = lambda *a, **kw: BrokenCollector()
    auxiliary_calls = []
    def auxiliary(category, **kw):
        auxiliary_calls.append(category)
        return replace(kw_source, category_id=category, state='blocked', blockers=['auxiliary_production_failed'])
    kw_source = runtime[3]
    worker.auxiliary = auxiliary
    worker.tick()
    assert auxiliary_calls == ['market_radar'] and not sends
    assert all(s['job']['status'] == 'blocked' for s in worker.status()['subscriptions'])
    assert 'PRIVATE_SENTINEL' not in json.dumps(worker.status(), default=str)


def test_live_config_provider_reloaded_at_each_execution_boundary(runtime):
    worker, publisher, sub, _, _, _, _ = runtime
    activate(worker, sub)
    provider_calls, configs = [], []
    def provider():
        value = object()
        provider_calls.append(value)
        return value
    worker.config_provider = provider
    worker.publisher_factory = lambda service, config, **kw: configs.append(config) or publisher
    # A retry creates a second execution boundary; no startup object is retained.
    source = runtime[3]
    class Collector:
        def collect(self, ids, *a, **kw):
            return {key: replace(source, state='blocked', blockers=['source_timeout']) for key in ids}
    worker.collector_factory = lambda *a, **kw: Collector()
    worker.tick()
    runtime[4][0] += timedelta(minutes=10)
    worker.tick()
    assert len(configs) == 2 and configs == provider_calls and configs[0] is not configs[1]


def test_pending_publication_survives_same_epoch_lease_takeover(runtime):
    worker, publisher, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    original_deliver = publisher.deliver
    publisher.deliver = lambda *a, **kw: None  # Crash after atomic prepare, before pending claim.
    worker.tick()
    old_publication = job(worker)['publication_id']
    assert old_publication and not sends
    publisher.deliver = original_deliver
    clock[0] += timedelta(minutes=1)
    new_worker = ResearchAutomation(publisher.service, publisher.config, clock=lambda: clock[0],
        collector_factory=lambda *a, **kw: pytest.fail('must not reselect/recollect'),
        publisher_factory=lambda *a, **kw: publisher)
    new_worker.tick()
    assert job(new_worker)['publication_id'] == old_publication and len(sends) == 1 and len(calls) == 1
    new_worker.stop()


def test_pending_configuration_suppression_is_aligned(runtime):
    worker, publisher, sub, _, _, _, sends = runtime
    activate(worker, sub)
    publisher.deliver = lambda *a, **kw: None
    worker.tick()
    current = publisher.repository.get(DeliverySubscription, sub['id'])
    publisher.service.update_subscription(sub['id'], {'version': current['version'], 'top_n': 3})
    worker.tick()
    assert job(worker)['status'] == 'suppressed' and job(worker)['error_code'] == 'configuration_changed'
    assert not sends


def test_native_thread_heartbeat_renews_during_provider_and_stop_fences(runtime, monkeypatch):
    import src.services.research_delivery.automation as module
    worker, _, sub, source, clock, _, sends = runtime
    activate(worker, sub)
    entered, release, renewed = threading.Event(), threading.Event(), threading.Event()
    class Collector:
        def collect(self, ids, day, progress=None):
            entered.set()
            assert release.wait(3)
            return {key: source for key in ids}
    worker.collector_factory = lambda *a, **kw: Collector()
    monkeypatch.setattr(module, 'POLL_SECONDS', .01)
    original = worker._heartbeat
    def pulse():
        original()
        renewed.set()
    worker._heartbeat = pulse
    worker.start(); worker.start()
    assert entered.wait(3)
    clock[0] += timedelta(seconds=30)
    assert renewed.wait(3)
    assert worker.status()['worker_alive'] and worker.status()['running']
    worker.stop()
    assert not worker.status()['worker_alive']
    release.set()
    worker._thread.join(3)
    assert not worker._thread.is_alive() and not sends


def test_auxiliary_only_holiday_never_calls_producer(runtime):
    worker, publisher, _, _, _, _, sends = runtime
    sub = publisher.service.create_subscription(dict(name='etf', destination_id=runtime[2]['destination_id'],
        strategy_id='etf_rotation', llm_mode='off', planned_time='17:10'))
    worker.update(1, True, [sub['id']])
    class CalendarOnly:
        def trading_day(self, *a, **kw): return {'is_open': 0}
        def collect(self, *a, **kw): pytest.fail('no stock crawling for auxiliary-only calendar')
    worker.collector_factory = lambda *a, **kw: CalendarOnly()
    worker.auxiliary = lambda *a, **kw: pytest.fail('holiday must fence auxiliary')
    worker.tick(); worker.tick()
    state = next(s for s in worker.status()['subscriptions'] if s['subscription_id'] == sub['id'])
    assert state['job']['status'] == 'skipped' and state['job']['error_code'] == 'non_trading_day' and not sends


def test_manual_bypasses_only_planned_time(runtime):
    worker, _, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    clock[0] = clock[0].replace(hour=17, minute=5)
    worker.tick()
    assert not sends and len(calls) == 1
    worker.request_run('before-planned-01')
    worker.tick()
    assert len(sends) == 1 and len(calls) == 1


def test_yaml_version_cache_is_not_reused(runtime, monkeypatch):
    worker, publisher, sub, source, clock, calls, sends = runtime
    activate(worker, sub)
    publisher.store_batch(replace(source, strategy_version=source.strategy_version))
    original = publisher.service.strategies
    monkeypatch.setattr(publisher.service, 'strategies', lambda: [dict(r, version='new-version')
        if r['strategy_id'] == sub['strategy_id'] else r for r in original()])
    # New YAML version must trigger collection; old snapshot is never selected.
    worker.tick()
    assert len(calls) == 1 and not sends
    assert job(worker)['error_code'] == 'strategy_version_changed'


def test_additive_tables_preserve_old_sqlite_rows_and_control(runtime):
    from src.storage import ScreeningRun
    from src.repositories.research_delivery_repository import ResearchDeliveryRepository
    worker, publisher, sub, *_ = runtime
    activate(worker, sub)
    with publisher.repository.atomic() as session:
        session.add(ScreeningRun(run_id='legacy-preserved', strategy=sub['strategy_id'], market='cn', result_json='{}'))
    ResearchDeliveryRepository(publisher.repository.session_factory)
    with publisher.repository.session_factory() as session:
        assert session.scalar(select(ScreeningRun).where(ScreeningRun.run_id == 'legacy-preserved'))
        assert session.get(AutomationControl, 'main').version == 2


def test_startup_enabled_outside_window_has_heartbeat_without_catchup(runtime, monkeypatch):
    import src.services.research_delivery.automation as module
    worker, _, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    clock[0] = clock[0].replace(hour=0, minute=1)
    heartbeat = threading.Event()
    original = worker._claim
    def claim():
        result = original()
        heartbeat.set()
        return result
    worker._claim = claim
    monkeypatch.setattr(module, 'POLL_SECONDS', .01)
    worker.start()
    assert heartbeat.wait(3)
    worker.stop()
    assert not calls and not sends and job(worker) is None


def test_parallel_manual_and_scheduled_tick_share_daily_identity(runtime):
    worker, _, sub, _, _, calls, sends = runtime
    activate(worker, sub)
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(worker.tick), pool.submit(worker.request_run, 'parallel-daily-01')]
        for result in results: result.result()
    worker.tick()
    assert len(calls) == len(sends) == 1 and job(worker)['status'] == 'sent'


def test_one_stock_failure_does_not_stop_other_stock_success(runtime):
    worker, publisher, sub, source, _, calls, sends = runtime
    second_strategy = next(s for s in publisher.service.strategies() if s['strategy_id'] != sub['strategy_id'])
    second = publisher.service.create_subscription(dict(name='other-stock', destination_id=sub['destination_id'],
        strategy_id=second_strategy['strategy_id'], llm_mode='off', planned_time='17:10'))
    worker.update(1, True, [sub['id'], second['id']])
    class Collector:
        def collect(self, ids, *a, **kw):
            calls.append(ids)
            other_source = replace(source, category_id=second_strategy['strategy_id'], strategy_version=second_strategy['version'],
                result=dict(source.result, strategy=second_strategy['strategy_id'], strategy_version=second_strategy['version']))
            return {sub['strategy_id']: replace(source, state='blocked', blockers=['source_permission_denied']),
                second_strategy['strategy_id']: other_source}
    worker.collector_factory = lambda *a, **kw: Collector()
    worker.tick()
    assert len(calls) == len(sends) == 1
    states = {s['subscription_id']: s['job']['status'] for s in worker.status()['subscriptions']}
    assert states == {sub['id']: 'blocked', second['id']: 'sent'}


def test_explicit_selected_subset_preserves_unselected_activation(runtime):
    worker, publisher, sub, *_ = runtime
    activate(worker, sub)
    first_version = publisher.repository.get(DeliverySubscription, sub['id'])['version']
    second = publisher.service.create_subscription(dict(name='other', destination_id=sub['destination_id'],
        strategy_id='market_radar', llm_mode='off', planned_time='18:10'))
    worker.update(2, True, [second['id']])
    assert publisher.repository.get(DeliverySubscription, sub['id'])['enabled']
    assert publisher.repository.get(DeliverySubscription, sub['id'])['version'] == first_version
    assert publisher.repository.get(DeliverySubscription, second['id'])['version'] == second['version'] + 1


def test_terminal_outbox_alignment_does_not_write_old_authorization_epoch(runtime):
    worker, publisher, sub, _, _, _, _ = runtime
    activate(worker, sub)
    publisher.deliver = lambda *a, **kw: None
    worker.tick()
    with publisher.repository.atomic() as session:
        row = session.scalar(select(AutomationJob))
        original = row.updated_at
        session.get(DeliveryOutbox, row.delivery_id).status = 'suppressed'
        session.get(AutomationControl, 'main').version += 1
    worker.tick()
    with publisher.repository.session_factory() as session:
        row = session.scalar(select(AutomationJob))
        assert row.updated_at == original and row.status == 'prepared'


def test_additive_and_noop_controls_preserve_prepared_release_and_eventual_send(runtime):
    worker, publisher, sub, _, clock, calls, sends = runtime
    sub = publisher.service.update_subscription(sub['id'], {'version': sub['version'], 'planned_time': '18:00'})
    activate(worker, sub)
    original_deliver = publisher.deliver
    publisher.deliver = lambda *a, **kw: None
    clock[0] = clock[0].replace(hour=17, minute=5)
    worker.request_run('prepare-before-due')
    worker.tick()
    first = job(worker)
    assert first['status'] == 'prepared' and first['publication_id']
    with publisher.repository.session_factory() as session:
        control = session.get(AutomationControl, 'main')
        authority = (control.version, control.lease_token, control.lease_until)
        first_version = session.get(DeliverySubscription, sub['id']).version
    other = publisher.service.create_subscription(dict(name='new-later-group', destination_id=sub['destination_id'],
        strategy_id='market_radar', llm_mode='off', planned_time='20:30'))
    clock[0] = clock[0].replace(minute=10)
    assert worker.update(2, True, [other['id']])['version'] == 2
    assert worker.update(2, True, [sub['id'], other['id']])['version'] == 2
    with publisher.repository.session_factory() as session:
        control = session.get(AutomationControl, 'main')
        assert (control.version, control.lease_token, control.lease_until) == authority
        assert session.get(DeliverySubscription, sub['id']).version == first_version
        assert session.get(DeliverySubscription, other['id']).version == other['version'] + 1
        assert session.get(DeliveryOutbox, first['delivery_id']).status == 'pending'
    assert job(worker)['publication_id'] == first['publication_id'] and job(worker)['status'] == 'prepared'
    publisher.deliver = original_deliver
    clock[0] = clock[0].replace(hour=18, minute=0)
    worker.tick(); worker.tick()
    assert job(worker)['status'] == 'sent' and job(worker)['publication_id'] == first['publication_id']
    assert len(calls) == len(sends) == 1


def test_noop_during_blocked_llm_retains_authority_and_sends(runtime):
    worker, publisher, sub, _, _, _, sends = runtime
    activate(worker, sub)
    entered, release = threading.Event(), threading.Event()
    def explain(*a):
        entered.set()
        assert release.wait(3)
        return {'status': 'off', 'reason': 'off', 'explanation': None}
    publisher.explanations.explain = explain
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.tick)
        assert entered.wait(3)
        token = worker._lease_token
        try:
            state = worker.update(2, True, [sub['id']])
            assert state['version'] == 2 and worker._lease_token == token
        finally:
            release.set()
        future.result()
    assert job(worker)['status'] == 'sent' and len(sends) == 1


def test_pause_revokes_pending_and_reenable_does_not_rebind(runtime):
    worker, publisher, sub, _, _, calls, sends = runtime
    activate(worker, sub)
    deliver = publisher.deliver
    publisher.deliver = lambda *a, **kw: None
    worker.tick()
    first = job(worker)
    assert first['status'] == 'prepared'
    assert worker.update(2, False, [])['version'] == 3
    assert worker.update(3, True, [sub['id']])['version'] == 4
    publisher.deliver = deliver
    worker.tick()
    assert job(worker)['status'] == 'suppressed'
    assert job(worker)['publication_id'] == first['publication_id'] and len(calls) == 1 and not sends


def test_injected_service_config_never_selects_global_config(runtime, monkeypatch):
    worker, publisher, *_ = runtime
    injected = object()
    monkeypatch.setattr(publisher.service, 'config', injected, raising=False)
    isolated = ResearchAutomation(publisher.service)
    assert isolated.config_provider() is injected


def test_shared_running_uses_durable_current_authority(runtime):
    worker, publisher, sub, source, clock, _, _ = runtime
    activate(worker, sub)
    observer = ResearchAutomation(publisher.service, publisher.config, clock=lambda: clock[0])
    entered, release = threading.Event(), threading.Event()
    class Collector:
        def collect(self, ids, *a, **kw):
            entered.set()
            assert release.wait(3)
            return {key: source for key in ids}
    worker.collector_factory = lambda *a, **kw: Collector()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.tick)
        assert entered.wait(3)
        try:
            assert observer.status()['running'] is True
        finally:
            release.set()
        future.result()
    assert observer.status()['running'] is False
    # Durable stale activity is not active without exact lease and epoch.
    with publisher.repository.atomic() as session:
        row = session.scalar(select(AutomationJob))
        row.status, row.stage = 'collecting', 'collecting'
        control = session.get(AutomationControl, 'main')
        control.lease_until = worker._utc(clock[0]) - timedelta(seconds=1)
    assert observer.status()['running'] is False
    with publisher.repository.atomic() as session:
        control = session.get(AutomationControl, 'main')
        control.lease_until = worker._utc(clock[0]) + timedelta(seconds=45)
        control.version += 1
    assert observer.status()['running'] is False


@pytest.mark.parametrize('next_day', [False, True])
def test_status_projects_abandoned_sending_outside_window_readonly(runtime, next_day):
    worker, publisher, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    publisher.deliver = lambda *a, **kw: None
    worker.tick()
    with publisher.repository.atomic() as session:
        row = session.scalar(select(AutomationJob))
        row.status, row.stage = 'sending', 'delivery'
        original = (row.status, row.stage, row.error_code, row.updated_at, row.lease_token)
        outbox = session.get(DeliveryOutbox, row.delivery_id)
        outbox.status, outbox.sending_at = 'sending', worker._utc(clock[0])
        outbox.attempt_count = 1
    worker.update(2, False, [])  # In-flight sending is not withdrawn by pause.
    clock[0] += timedelta(seconds=89)
    assert job(worker)['status'] == 'sending'
    clock[0] += timedelta(seconds=1)
    assert job(worker)['status'] == 'delivery_unknown'
    clock[0] = clock[0] + timedelta(days=1) if next_day else clock[0].replace(hour=22, minute=31)
    state = job(worker)
    assert state['status'] == 'delivery_unknown' and state['stage'] == 'delivery'
    assert state['error_code'] == 'abandoned_sending'
    with publisher.repository.session_factory() as session:
        row = session.scalar(select(AutomationJob))
        assert (row.status, row.stage, row.error_code, row.updated_at, row.lease_token) == original
        assert session.get(DeliveryOutbox, row.delivery_id).status == 'sending'
    worker.tick()
    assert len(calls) == 1 and not sends


def test_additive_enables_commute_without_canceling_scheduled_ready_job(runtime):
    worker, publisher, sub, _, clock, calls, sends = runtime
    sub = publisher.service.update_subscription(sub['id'], {'version': sub['version'], 'planned_time': '18:00'})
    activate(worker, sub)
    clock[0] = clock[0].replace(hour=17, minute=5)
    worker.tick()
    assert job(worker)['status'] == 'ready' and not sends
    batch_id = job(worker)['batch_id']
    additions = [publisher.service.create_subscription(dict(name=category, destination_id=sub['destination_id'],
        strategy_id=category, llm_mode='off', planned_time='20:30')) for category in ('market_radar', 'etf_rotation')]
    clock[0] = clock[0].replace(minute=10)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda addition: worker.update(2, True, [addition['id']]), additions))
    assert all(result['version'] == 2 for result in results)
    assert job(worker)['status'] == 'ready' and job(worker)['batch_id'] == batch_id
    clock[0] = clock[0].replace(hour=18, minute=0)
    worker.tick(); worker.tick()
    assert job(worker)['status'] == 'sent' and len(calls) == len(sends) == 1


@pytest.mark.parametrize('outcome', ['sent', 'rejected', 'suppressed', 'delivery_unknown'])
def test_status_projects_terminal_outbox_without_job_write_or_authority(runtime, outcome):
    worker, publisher, sub, _, clock, calls, sends = runtime
    activate(worker, sub)
    publisher.deliver = lambda *a, **kw: None
    worker.tick()
    with publisher.repository.atomic() as session:
        row = session.scalar(select(AutomationJob))
        original = (row.status, row.stage, row.error_code, row.updated_at)
        outbox = session.get(DeliveryOutbox, row.delivery_id)
        outbox.status, outbox.error_code, outbox.completed_at = outcome, 'safe_outcome', worker._utc(clock[0])
        session.get(AutomationControl, 'main').version += 1
    clock[0] += timedelta(days=1)
    assert job(worker)['status'] == outcome and job(worker)['error_code'] == 'safe_outcome'
    assert job(worker)['stage'] == 'delivery'
    with publisher.repository.session_factory() as session:
        row = session.scalar(select(AutomationJob))
        assert (row.status, row.stage, row.error_code, row.updated_at) == original
    assert len(calls) == 1 and not sends


def test_optional_enrichment_failure_preserves_single_base_publication(runtime):
    worker, publisher, sub, _, _, calls, sends = runtime
    activate(worker, sub)
    worker.enricher_factory = lambda *a, **kw: SimpleNamespace(enrich=Mock(side_effect=RuntimeError('PRIVATE_SENTINEL')))
    worker.tick(); worker.tick()
    assert job(worker)['status'] == 'sent' and len(calls) == len(sends) == 1
    with publisher.repository.session_factory() as session:
        data = json.loads(session.scalar(select(ResearchBatch)).result_json)
    assert data['result']['research_context']['statuses']['background'] == 'enrichment_failed'
    assert 'PRIVATE_SENTINEL' not in str(data)


def test_all_completed_collections_available_before_first_enrichment_and_shared_symbol_request(runtime):
    worker, publisher, sub, original, clock, calls, sends = runtime
    strategy = next(s for s in publisher.service.strategies() if s['strategy_id'] != sub['strategy_id'])
    second = publisher.service.create_subscription(dict(name='other', destination_id=sub['destination_id'],
        strategy_id=strategy['strategy_id'], llm_mode='off', planned_time='17:10', top_n=3))
    worker.update(1, True, [sub['id'], second['id']])
    fetch = Mock(return_value={})
    instances = []
    def factory(config, **kw):
        instance = ResearchContextEnricher(config, **kw, fundamental_fetcher=fetch,
            news_fetcher=Mock(return_value=SimpleNamespace(success=True, results=[])),
            announcement_fetcher=Mock(return_value=dict(ok=True, records=[], fetched_at=clock[0].isoformat())))
        instances.append(instance)
        return instance
    worker.enricher_factory = factory
    class Collector:
        def collect(self, ids, *a, **kw):
            calls.append(ids)
            return {key: replace(original, category_id=key,
                strategy_version=strategy['version'] if key == strategy['strategy_id'] else original.strategy_version,
                result=dict(original.result, strategy=key, strategy_version=strategy['version'] if key == strategy['strategy_id'] else original.strategy_version)) for key in ids}
    worker.collector_factory = lambda *a, **kw: Collector()
    worker.tick(); worker.tick()
    assert len(instances) == fetch.call_count == 1 and len(sends) == 2
    with publisher.repository.session_factory() as session:
        rows = list(session.scalars(select(ResearchBatch)))
        assert len(rows) == 2
        for row in rows:
            context = json.loads(row.result_json)['result']['research_context']
            assert context['compared_strategies'] == sorted([sub['strategy_id'], strategy['strategy_id']])
            assert context['cross_strategy']['600001'] == context['compared_strategies']


@pytest.mark.parametrize('code', ['lease_lost', 'authorization_changed'])
def test_enrichment_progress_cancellation_never_publishes_base(runtime, code):
    worker, publisher, sub, _, _, calls, sends = runtime
    activate(worker, sub)
    def enrich(result, *, progress, **kw):
        progress()
        pytest.fail('progress must interrupt enrichment')
    worker.enricher_factory = lambda *a, **kw: SimpleNamespace(enrich=enrich)
    original = worker._progress
    count = [0]
    def progress(*a, **kw):
        count[0] += 1
        if count[0] >= 5:  # Inside optional enrichment's progress callback.
            raise ManagementError(code, 'guard', 409)
        original(*a, **kw)
    worker._progress = progress
    worker.tick()
    assert not sends
    with publisher.repository.session_factory() as session:
        assert session.query(ResearchBatch).count() == 0


@pytest.mark.parametrize('historical_count', [1, 100])
def test_restart_recovers_old_version_requests_without_old_version_comparisons(runtime, monkeypatch, historical_count):
    worker, publisher, sub, source, clock, calls, sends = runtime
    other = next(s for s in publisher.service.strategies() if s['strategy_id'] != sub['strategy_id'])
    second = publisher.service.create_subscription(dict(name='old-version', destination_id=sub['destination_id'],
        strategy_id=other['strategy_id'], llm_mode='off', planned_time='17:10'))
    directory = publisher.service.vault.root.parent / 'research-delivery-evidence'
    def build(config, **kw):
        return ResearchContextEnricher(config, **kw, fundamental_fetcher=Mock(return_value={}),
            news_fetcher=Mock(return_value=SimpleNamespace(success=True, results=[])),
            announcement_fetcher=Mock(return_value=dict(ok=True, records=[], fetched_at=clock[0].isoformat())))
    history = build(publisher.config, evidence_dir=directory, clock=lambda: clock[0])
    captures = []
    for start in range(0, historical_count, 20):
        picks = [dict(source.result['picks'][0], code=str(600001 + index), rank=index + 1)
            for index in range(start, min(start + 20, historical_count))]
        enriched = history.enrich(replace(source, result=dict(source.result, picks=picks)),
            related_results=[], top_n_by_strategy={source.category_id: 20})
        captures.extend(entry for entry in enriched.evidence if entry not in captures)
    frozen = replace(source, category_id=other['strategy_id'], strategy_version=other['version'], evidence=captures,
        result=dict(source.result, strategy=other['strategy_id'], strategy_version=other['version']))
    saved = publisher.store_batch(frozen)
    original_rows = publisher.service.strategies()
    monkeypatch.setattr(publisher.service, 'strategies', lambda: [dict(row, version='next-version')
        if row['strategy_id'] == other['strategy_id'] else row for row in original_rows])
    worker.update(1, True, [sub['id'], second['id']])
    current_code = '600001' if historical_count == 1 else '600999'
    current = replace(source, result=dict(source.result,
        picks=[dict(source.result['picks'][0], code=current_code)]))
    class Collector:
        def collect(self, ids, *a, **kw):
            calls.append(ids)
            return {source.category_id: current, other['strategy_id']: replace(frozen,
                strategy_version='next-version', state='blocked', blockers=['source_permission_denied'])}
    worker.collector_factory = lambda *a, **kw: Collector()
    worker.enricher_factory = build
    worker.tick()
    assert len(calls) == len(sends) == 1
    assert worker._enricher.fundamental_fetcher.call_count == 0
    with publisher.repository.session_factory() as session:
        old = session.get(ResearchBatch, saved['id'])
        assert json.loads(old.result_json) == frozen.to_dict()  # No frozen rewrite.
        new = session.scalar(select(ResearchBatch).where(ResearchBatch.category_id == source.category_id))
        context = json.loads(new.result_json)['result']['research_context']
    assert context['background_scope']['requested_count'] == historical_count
    assert context['compared_strategies'] == [source.category_id]
    assert context['cross_strategy'][current_code] == [source.category_id]
    if historical_count == 100:
        assert context['background'][current_code]['status'] == 'daily_limit'
