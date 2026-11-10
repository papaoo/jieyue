"""Isolated formal publication contract, including the durable network boundary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from tests.test_research_delivery_aggregation import research
from tests.test_research_delivery_outbox import enabled_target
from src.notification_sender.feishu_sender import DeliveryOutcome
from src.repositories.automation_models import AutomationControl, AutomationJob, PublicationRelease, ResearchBatch
from src.repositories.research_delivery_models import DeliveryDestination, DeliveryOutbox, ResearchPublication
from src.repositories.research_delivery_repository import ManagementError
from src.services.research_delivery.evidence_store import EvidenceStore
from src.services.research_delivery.formal_publisher import FormalPublisher
from src.services.research_delivery.live_contracts import ProducerResult


@pytest.fixture
def formal(research, tmp_path):
    manual, sub, dest = research
    enabled_target(manual, dest)
    sub = manual.service.update_subscription(sub['id'], {'version': sub['version'], 'enabled': True})
    stamp = datetime(2026, 10, 15, 18, tzinfo=ZoneInfo('Asia/Shanghai'))
    clock = [stamp]
    publisher = FormalPublisher(manual.service, manual.explanations.config, clock=lambda: clock[0])
    with publisher.repository.atomic() as session:
        session.add(AutomationControl(id='main', enabled=True, version=1, owner_id='test', lease_token='lease',
                                     lease_until=stamp.replace(tzinfo=None) - timedelta(hours=8) + timedelta(hours=1)))
    evidence = EvidenceStore(tmp_path / 'evidence').capture_payload('test', 'snapshot', {'public': True},
        fetched_at=stamp, business_date=stamp.date(), provenance='provider_normalized')
    result = ProducerResult(sub['strategy_id'], stamp.date().isoformat(), 'degraded',
        next(r['version'] for r in manual.service.strategies() if r['strategy_id'] == sub['strategy_id']),
        result={'strategy': sub['strategy_id'], 'strategy_version': next(r['version'] for r in manual.service.strategies()
                if r['strategy_id'] == sub['strategy_id']), 'market': 'cn', 'ranking_mode': 'factor', 'llm_ranked': False,
                'run_id': 'run-safe', 'picks': [dict(code='600001', name='示例', rank=1, final_score=80,
                screen_score=75, price=10, ranking_reason='量价条件符合规则', risk_flags=['波动'], risk_level='medium')]},
        coverage=dict(scope='restricted_complete_core_metric_eligible_universe', independent_universe_count=100,
            calendar=dict(exchange='SSE', business_date='2026-10-15', is_open=1),
            daily_response_count=100, daily_basic_response_count=100, daily_response_coverage=1.,
            daily_basic_response_coverage=1., eligible_count=98, required_count=10, valid_count=10,
            technical_limit=100, technical_not_evaluated_count=0, unknown_missing_count=0),
        evidence=[evidence], warnings=['restricted_sample'])
    yield publisher, sub, result, clock


def batch(formal):
    publisher, sub, source, _ = formal
    saved = publisher.store_batch(source)
    return publisher.prepare(sub['id'], saved['id'], lease_token='lease')


def test_formal_preparation_uses_v4_catalog_and_freezes_role_adoption(formal):
    from tests.test_research_delivery_explanation import model_config
    from tests.test_research_delivery_signals import selection
    from src.services.research_delivery.explanation import ExplanationService
    from src.services.research_delivery.brief_rendering import frozen_payload
    publisher, sub, _, _ = formal
    publisher.service.update_subscription(sub['id'], {'version': sub['version'], 'llm_mode': 'explain_only'})
    calls = []
    def complete(prompt, *args, **kwargs):
        projected = json.loads(prompt.removeprefix('<frozen_input>').removesuffix('</frozen_input>'))
        calls.append(projected)
        assert projected['strategy']['expression_version'] == 4 and projected['signals']
        return json.dumps(selection(projected)), 'openai/vendor/model', {}
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=complete)
    report = batch(formal)
    assert report['llm']['status'] == 'succeeded' and len(calls) == 1
    assert report['model_adoption']['candidate_rationale_count'] == 1
    assert report['model_adoption']['summary'] == 1 and report['model_adoption']['core_complete']
    assert frozen_payload(report) == report['card_payload']


def test_formal_identity_atomic_and_single_send(formal):
    publisher, sub, result, _ = formal
    saved = publisher.store_batch(result)
    with ThreadPoolExecutor(2) as pool:
        publications = list(pool.map(lambda _: publisher.prepare(sub['id'], saved['id'], lease_token='lease'), range(2)))
    assert len({r['id'] for r in publications}) == 1
    with publisher.repository.session_factory() as session:
        assert session.query(ResearchPublication).count() == session.query(PublicationRelease).count() == 1
        assert session.query(DeliveryOutbox).count() == session.query(AutomationJob).count() == 1
        job = session.scalar(select(AutomationJob))
        assert job.publication_id == publications[0]['id'] and job.batch_id == saved['id'] and job.delivery_id
    calls = []
    def send(credentials, payload):
        with publisher.repository.session_factory() as session:
            assert session.scalar(select(DeliveryOutbox)).status == 'sending'
        calls.append(payload)
        return DeliveryOutcome('sent', None)
    publisher.sender = send
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: publisher.deliver(publications[0]['id'], lease_token='lease'), range(2)))
    assert len(calls) == 1
    assert publisher.deliver(publications[0]['id'], lease_token='lease')['status'] == 'sent'
    assert calls[0]['msg_type'] == 'interactive'
    text = calls[0]['card']['elements'][0]['text']['content']
    assert all(x in text for x in ('2026-10-15', '波动', '量价条件', '非投资建议', '限定'))
    assert publications[0]['envelope']['candidates'][0]['metrics']['final_score'] == 80
    assert '人工' not in text and '测试' not in text


def test_brief_deliver_is_frozen_and_keyword_is_visible(formal, monkeypatch):
    publisher, sub, source, _ = formal
    report = batch(formal)
    assert report['format_version'] == 'research-brief-v1'
    assert report['brief_input']['strategy']['expression_version'] == 4
    assert report['card_payload']['card']['elements'][0]['text']['content'] == report['rendered_content']
    assert '阶跃' in report['rendered_content']
    monkeypatch.setattr('src.services.research_delivery.formal_publisher.build_brief_input', lambda *a: pytest.fail('delivery must not rebuild facts'))
    publisher.explanations.explain = lambda *a: pytest.fail('delivery must not call model')
    sent = []
    publisher.sender = lambda creds, payload: sent.append(payload) or DeliveryOutcome('sent', None)
    assert publisher.deliver(report['id'], lease_token='lease')['status'] == 'sent'
    assert sent == [report['card_payload']]


def test_card_tamper_even_with_rehashed_frozen_rows_is_rejected(formal):
    from src.services.research_delivery.aggregation import canonical, content_hash
    publisher, _, _, _ = formal
    report = batch(formal)
    with publisher.repository.atomic() as session:
        pub = session.get(ResearchPublication, report['id'])
        release = session.get(PublicationRelease, report['id'])
        outbox = session.scalar(select(DeliveryOutbox))
        doc = json.loads(pub.document_json)
        doc['card_payload']['card']['elements'][0]['text']['content'] += '篡改'
        payload = doc['card_payload']
        pub.document_json, pub.content_hash = canonical(doc), content_hash(doc)
        release.payload_json, release.payload_hash = canonical(payload), content_hash(payload)
        outbox.payload_json, outbox.payload_hash = canonical(payload), content_hash(payload)
    publisher.sender = lambda *a: pytest.fail('tampered card must not send')
    assert publisher.deliver(report['id'], lease_token='lease')['error_code'] == 'payload_changed'


def test_historical_text_release_remains_deliverable(formal):
    from src.services.research_delivery.aggregation import canonical, content_hash
    publisher, _, _, _ = formal
    report = batch(formal)
    with publisher.repository.atomic() as session:
        pub = session.get(ResearchPublication, report['id'])
        release = session.get(PublicationRelease, report['id'])
        outbox = session.scalar(select(DeliveryOutbox))
        document = json.loads(pub.document_json)
        for key in ('format_version', 'card_payload', 'brief_input', 'title'):
            document.pop(key, None)
        payload = dict(msg_type='text', content=dict(text=document['rendered_content']))
        pub.document_json, pub.content_hash = canonical(document), content_hash(document)
        release.payload_json, release.payload_hash = canonical(payload), content_hash(payload)
        outbox.payload_json, outbox.payload_hash = canonical(payload), content_hash(payload)
    sent = []
    publisher.sender = lambda creds, payload: sent.append(payload) or DeliveryOutcome('sent', None)
    assert publisher.deliver(report['id'], lease_token='lease')['status'] == 'sent'
    assert sent[0]['msg_type'] == 'text'


def test_unknown_never_resends_and_daily_batch_conflict(formal):
    publisher, sub, result, _ = formal
    report = batch(formal)
    calls = []
    publisher.sender = lambda *a: calls.append(a) or DeliveryOutcome('delivery_unknown', 'timeout')
    assert publisher.deliver(report['id'], lease_token='lease')['status'] == 'delivery_unknown'
    assert publisher.deliver(report['id'], lease_token='wrong')['status'] == 'delivery_unknown'
    assert publisher.prepare(sub['id'], publisher.store_batch(result)['id'], lease_token='wrong')['id'] == report['id']
    altered = publisher.store_batch(replace(result, warnings=['changed']))
    with pytest.raises(ManagementError, match='binding'):
        publisher.prepare(sub['id'], altered['id'], lease_token='lease')
    assert len(calls) == 1


@pytest.mark.parametrize('change,code', [
    ({'state': 'blocked'}, 'batch_blocked'), ({'state': 'invented'}, 'invalid_batch'),
    ({'state': []}, 'invalid_batch'),
    ({'strategy_version': 'old'}, 'strategy_version_changed'), ({'business_date': '2026-10-14'}, 'business_date_mismatch'),
    ({'coverage': {}}, 'coverage_invalid'), ({'evidence': []}, 'evidence_invalid'),
])
def test_untrusted_or_invalid_result_rejected(formal, change, code):
    publisher, _, result, _ = formal
    with pytest.raises(ManagementError) as error:
        publisher.store_batch(replace(result, **change))
    assert error.value.code == code
    with pytest.raises(ManagementError):
        publisher.store_batch(result.to_dict())


@pytest.mark.parametrize('mutation,code', [('batch', 'batch_hash_changed'), ('payload', 'payload_changed'),
    ('pause', 'automation_paused'), ('epoch', 'authorization_changed'), ('expiry', 'batch_expired')])
def test_deliver_rechecks_frozen_guards(formal, mutation, code):
    publisher, _, _, _ = formal
    report = batch(formal)
    with publisher.repository.atomic() as session:
        control = session.get(AutomationControl, 'main')
        if mutation == 'batch':
            session.scalar(select(ResearchBatch)).result_json = '{}'
        elif mutation == 'payload':
            session.scalar(select(DeliveryOutbox)).payload_json = '{}'
        elif mutation == 'pause': control.enabled = False
        elif mutation == 'epoch': control.version += 1
        elif mutation == 'expiry': session.scalar(select(ResearchBatch)).expires_at = datetime(2026, 10, 15, 9)
    publisher.sender = lambda *a: pytest.fail('guard must precede sending')
    result = publisher.deliver(report['id'], lease_token='lease')
    assert result['status'] == 'suppressed' and result['error_code'] == code


def test_pause_during_llm_prevents_final_save(formal):
    publisher, sub, source, _ = formal
    saved = publisher.store_batch(source)
    def explain(*a):
        with publisher.repository.atomic() as session:
            session.get(AutomationControl, 'main').enabled = False
        return {'status': 'off', 'explanation': None}
    publisher.explanations.explain = explain
    with pytest.raises(ManagementError) as error:
        publisher.prepare(sub['id'], saved['id'], lease_token='lease')
    assert error.value.code == 'automation_paused'
    with publisher.repository.session_factory() as session:
        assert session.query(ResearchPublication).count() == 0


def test_redacted_document_small_prompt_and_no_llm_reordering(formal):
    publisher, sub, source, _ = formal
    source.result['saved_path'] = 'C:/private/token-secret.json'
    source.result['picks'][0]['dsa_context'] = {'api_key': 'secret', 'path': 'C:/secret'}
    source.coverage['independent_universe_codes'] = ['600001'] * 6000
    captured = []
    def explain(envelope, *args):
        captured.append(envelope)
        return {'status': 'fallback', 'reason': 'invalid_model_output', 'explanation': None}
    publisher.explanations.explain = explain
    report = batch(formal)
    public = json.dumps(report, default=str, ensure_ascii=False)
    assert all(x not in public for x in ('C:/', 'private', 'token-secret', 'api_key', 'independent_universe_codes', 'credential_ref'))
    assert len(json.dumps(captured[0])) < 12000
    assert report['envelope']['candidates'][0]['code'] == '600001'
    assert report['llm']['reason'] == 'invalid_model_output'


def test_window_and_destination_rotation(formal):
    publisher, sub, source, clock = formal
    report = batch(formal)
    target = publisher.repository.get(DeliveryDestination, sub['destination_id'])
    publisher.service.update_destination(target['id'], {'version': target['version'], 'enabled': False})
    assert publisher.deliver(report['id'], lease_token='lease')['status'] == 'suppressed'
    clock[0] = clock[0].replace(hour=22, minute=30)
    with pytest.raises(ManagementError) as error:
        publisher.store_batch(source)
    assert error.value.code == 'outside_publication_window'


def test_readiness_counts_do_not_assume_unproven_not_applicable(formal):
    publisher, _, source, _ = formal
    source.coverage['valid_count'] -= 1
    with pytest.raises(ManagementError) as error:
        publisher.store_batch(source)
    assert error.value.code == 'coverage_invalid'


def test_oversize_is_explicit_without_candidate_truncation(formal):
    publisher, sub, source, _ = formal
    for i in range(1, 5):
        source.result['picks'].append(dict(source.result['picks'][0], code=f'{600001 + i}', rank=i + 1))
    for row in source.result['picks']:
        row['risk_summary'] = '风险尚待核实' * 700
    saved = publisher.store_batch(source)
    with pytest.raises(ManagementError) as error:
        publisher.prepare(sub['id'], saved['id'], lease_token='lease')
    assert error.value.code == 'payload_too_large'
    with publisher.repository.session_factory() as session:
        assert session.query(ResearchPublication).count() == session.query(DeliveryOutbox).count() == 0


def test_existing_database_additive_registration_defaults(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker
    from src.repositories.research_delivery_repository import ResearchDeliveryRepository
    engine = create_engine('sqlite:///' + (tmp_path / 'legacy.db').as_posix())
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE unrelated_history (value TEXT)'))
        conn.execute(text("INSERT INTO unrelated_history VALUES ('preserved')"))
    factory = sessionmaker(bind=engine)
    ResearchDeliveryRepository(factory)
    assert ResearchBatch.__tablename__ in inspect(engine).get_table_names()
    with factory.begin() as session:
        session.add(AutomationControl())
    with factory() as session:
        control = session.get(AutomationControl, 'main')
        assert control.enabled is False and control.version == 1
        assert session.scalar(text('SELECT value FROM unrelated_history')) == 'preserved'
    engine.dispose()


@pytest.mark.parametrize('damage', ['order', 'number', 'timeout'])
def test_llm_fallback_preserves_deterministic_facts_and_once_identity(formal, damage):
    from tests.test_research_delivery_explanation import model_config
    from src.services.research_delivery.explanation import ExplanationService
    publisher, sub, source, _ = formal
    sub = publisher.service.update_subscription(sub['id'], {'version': sub['version'], 'llm_mode': 'explain_only'})
    calls = []
    def completion(prompt, *a, **kw):
        calls.append(prompt)
        if damage == 'timeout':
            raise TimeoutError('token=must-not-be-stored')
        row = dict(code='600999' if damage == 'order' else '600001', reasons=[])
        if damage == 'number':
            row['reasons'] = [dict(kind='inference', text='可能上涨九倍', metric_refs=['price'], evidence_ids=[])]
        return json.dumps(dict(candidates=[row])), 'openai/vendor/model', {}
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=completion)
    saved = publisher.store_batch(source)
    first = publisher.prepare(sub['id'], saved['id'], lease_token='lease')
    second = publisher.prepare(sub['id'], saved['id'], lease_token='lease')
    assert len(calls) == 1 and first['id'] == second['id']
    assert first['llm']['status'] == 'fallback'
    assert first['envelope']['candidates'][0]['metrics']['final_score'] == 80
    assert 'must-not-be-stored' not in json.dumps(first, default=str)


def test_concurrent_prepares_reserve_same_model_input_once(formal):
    from threading import Barrier, Event
    from tests.test_research_delivery_explanation import model_config
    from src.services.research_delivery.explanation import ExplanationService
    publisher, sub, source, _ = formal
    sub = publisher.service.update_subscription(sub['id'], {'version': sub['version'], 'llm_mode': 'explain_only'})
    calls, gate = [], Barrier(2)
    done = Event()
    def completion(*a, **k):
        calls.append(1)
        done.wait(timeout=5)
        return '{"candidates":[{"code":"600001","reasons":[]}]}', 'openai/vendor/model', {}
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=completion)
    explain = publisher.explanations.explain
    def synchronized(*a):
        gate.wait(timeout=5)
        result = explain(*a)
        if result.get('reason') == 'attempt_in_progress_or_unknown':
            done.set()
        return result
    publisher.explanations.explain = synchronized
    saved = publisher.store_batch(source)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: publisher.prepare(sub['id'], saved['id'], lease_token='lease'), range(2)))
    assert len(calls) == 1 and len({r['id'] for r in results}) == 1


def test_pause_after_durable_send_claim_cannot_revoke_inflight(formal):
    publisher, _, _, _ = formal
    report = batch(formal)
    def send(*a):
        with publisher.repository.atomic() as session:
            session.get(AutomationControl, 'main').enabled = False
        return DeliveryOutcome('sent', None)
    publisher.sender = send
    assert publisher.deliver(report['id'], lease_token='lease')['status'] == 'sent'


def test_evidence_bytes_changed_after_prepare_are_rejected(formal):
    publisher, _, source, _ = formal
    report = batch(formal)
    from pathlib import Path
    # Simulate a tampered capture without modifying a real producer asset.
    Path(source.evidence[0]['path']).unlink()
    result = publisher.deliver(report['id'], lease_token='lease')
    assert result['status'] == 'suppressed' and result['error_code'] == 'evidence_invalid'


@pytest.mark.parametrize('category', ['etf_rotation', 'market_radar', 'announcement_risk'])
def test_actual_auxiliary_producer_adapters_retain_limits_order_and_evidence(formal, tmp_path, monkeypatch, category):
    from tests.test_research_delivery_auxiliary import run, ETFManager, hotspot, theme, announcement, envelope
    from tests.test_research_delivery_explanation import model_config
    from src.services.research_delivery.explanation import ExplanationService
    import pandas as pd
    publisher, sub, _, _ = formal
    sub = publisher.service.update_subscription(sub['id'], dict(version=sub['version'], strategy_id=category,
        top_n=2, llm_mode='explain_only'))
    if category == 'etf_rotation':
        monkeypatch.setattr('src.core.trading_calendar.get_trading_dates', lambda market, start, end: pd.bdate_range(start, end))
        source = run(tmp_path, category, fetcher_manager=ETFManager())
    elif category == 'market_radar':
        source = run(tmp_path, category, hotspot_service=hotspot(leading_industries=[theme()]))
    else:
        from unittest.mock import Mock
        source = run(tmp_path, category, announcement_fetcher=Mock(return_value=envelope([
            announcement(title='公告甲'), announcement(title='公告乙', url='https://static.cninfo.com.cn/finalpage/2026-10-15/b.pdf'),
            announcement(title='公告丙', url='https://static.cninfo.com.cn/finalpage/2026-10-15/c.pdf')
        ])))
    captured = []
    def completion(prompt, *a, **kw):
        captured.append(prompt)
        frozen = json.loads(prompt.removeprefix('<frozen_input>').removesuffix('</frozen_input>'))
        from tests.test_research_delivery_brief import reply
        return json.dumps(reply(frozen)), 'openai/vendor/model', {}
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=completion)
    saved = publisher.store_batch(source)
    report = publisher.prepare(sub['id'], saved['id'], lease_token='lease')
    assert len(captured) == 1 and report['llm']['status'] == 'fallback'
    assert report['llm']['reason'] == 'no_effective_model_paragraphs'
    assert 'path' not in captured[0] and 'sha256' not in captured[0]
    if category == 'announcement_risk':
        assert len(report['envelope']['candidates']) == 1 and len(report['envelope']['evidence']) == 2
        assert report['detail']['records'] == [
            {k: row[k] for k in ('issuer_code', 'title', 'published_date', 'source', 'url', 'evidence_kind')}
            for row in source.result['records'][:2]]
        assert report['detail']['shown_count'] == 2 and report['detail']['total_count'] == 3
        assert '正文、金额与情绪未经核实' in report['rendered_content']
    else:
        assert [row['code'] for row in report['envelope']['candidates']] == [row['id'] for row in source.result['items'][:2]]
        if category == 'etf_rotation':
            assert report['detail']['research_weights'] == source.result['target_weights']
            assert '区间动量' in report['rendered_content'] and '个交易日' in report['rendered_content'] and '非交易指令' in report['rendered_content']
            assert '小数比例' not in report['rendered_content']
        else:
            assert '概念排名数据缺失' in report['rendered_content']


def test_empty_verified_announcement_scope_explicit_no_model_fallback(formal, tmp_path):
    from tests.test_research_delivery_auxiliary import run, envelope
    from unittest.mock import Mock
    publisher, sub, _, _ = formal
    sub = publisher.service.update_subscription(sub['id'], dict(version=sub['version'], strategy_id='announcement_risk', llm_mode='explain_only'))
    source = run(tmp_path, 'announcement_risk', announcement_fetcher=Mock(return_value=envelope()))
    publisher.explanations.explain = lambda *a: pytest.fail('no supported rows must not call model')
    report = publisher.prepare(sub['id'], publisher.store_batch(source)['id'], lease_token='lease')
    assert report['llm']['status'] == 'fallback' and report['llm']['reason'] == 'no_supported_evidence'
    assert '不代表公司无风险' in report['rendered_content']


def test_manual_namespace_collision_and_stale_authorization_cannot_rebind(formal):
    publisher, sub, source, _ = formal
    report = batch(formal)
    with publisher.repository.atomic() as session:
        session.scalar(select(DeliveryOutbox)).kind = 'test'
    with pytest.raises(ManagementError) as error:
        publisher.prepare(sub['id'], publisher.store_batch(source)['id'], lease_token='lease')
    assert error.value.code == 'idempotency_kind_conflict'
    with publisher.repository.atomic() as session:
        session.scalar(select(DeliveryOutbox)).kind = 'formal'
        session.get(AutomationControl, 'main').version += 1
    with pytest.raises(ManagementError) as error:
        publisher.prepare(sub['id'], publisher.store_batch(source)['id'], lease_token='lease')
    assert error.value.code == 'authorization_changed'
    assert publisher.deliver(report['id'], lease_token='lease')['status'] == 'suppressed'


def test_crashed_sending_recovers_to_unknown_without_retry(formal):
    publisher, _, _, _ = formal
    report = batch(formal)
    with publisher.repository.atomic() as session:
        row = session.scalar(select(DeliveryOutbox))
        row.status, row.attempt_count = 'sending', 1
        row.sending_at = datetime(2026, 10, 15, 9, 58)
    publisher.sender = lambda *a: pytest.fail('abandoned request must never be sent again')
    result = publisher.deliver(report['id'], lease_token='lease')
    assert result['status'] == 'delivery_unknown' and result['error_code'] == 'abandoned_sending'
    assert publisher.deliver(report['id'], lease_token='lease') == result


@pytest.mark.parametrize('hour,minute,allowed', [(15, 29, False), (15, 30, True), (22, 29, True), (22, 30, False)])
def test_exact_shanghai_business_window(formal, hour, minute, allowed):
    publisher, _, source, clock = formal
    clock[0] = clock[0].replace(hour=hour, minute=minute)
    # 边界样本：窗口起点 15:30、终点 22:30（闭开区间）
    if allowed:
        assert publisher._window(source.business_date) == clock[0]
    else:
        with pytest.raises(ManagementError) as error:
            publisher._window(source.business_date)
        assert error.value.code == 'outside_publication_window'


@pytest.mark.parametrize('expired', [False, True])
def test_stale_lease_cannot_poison_pending_delivery_for_new_owner(formal, expired):
    publisher, _, _, _ = formal
    report = batch(formal)
    with publisher.repository.atomic() as session:
        control = session.get(AutomationControl, 'main')
        control.lease_token = 'new-owner'
        if expired:
            control.lease_until = datetime(2026, 10, 15, 9, 59)
        outbox = publisher.repository.delivery_public(session.scalar(select(DeliveryOutbox)))
        job = session.scalar(select(AutomationJob))
        before_job = (job.status, job.stage, job.lease_token, job.updated_at, job.error_code)
    with pytest.raises(ManagementError) as error:
        publisher.deliver(report['id'], lease_token='lease' if not expired else 'new-owner')
    assert error.value.code == 'lease_lost'
    with publisher.repository.session_factory() as session:
        assert publisher.repository.delivery_public(session.scalar(select(DeliveryOutbox))) == outbox
        job = session.scalar(select(AutomationJob))
        assert (job.status, job.stage, job.lease_token, job.updated_at, job.error_code) == before_job
    with publisher.repository.atomic() as session:
        control = session.get(AutomationControl, 'main')
        control.owner_id, control.lease_token, control.lease_until = 'new-worker', 'current', datetime(2026, 10, 15, 11)
    calls = []
    publisher.sender = lambda *a: calls.append(a) or DeliveryOutcome('sent', None)
    assert publisher.deliver(report['id'], lease_token='current')['status'] == 'sent'
    assert publisher.deliver(report['id'], lease_token='lease')['status'] == 'sent'
    assert len(calls) == 1


def test_lease_expiring_during_validation_rolls_back_claim(formal, monkeypatch):
    publisher, _, _, clock = formal
    report = batch(formal)
    resolve = publisher.service.vault.resolve
    def delayed_resolve(ref):
        credentials = resolve(ref)
        clock[0] = clock[0].replace(hour=19)
        return credentials
    monkeypatch.setattr(publisher.service.vault, 'resolve', delayed_resolve)
    with pytest.raises(ManagementError) as error:
        publisher.deliver(report['id'], lease_token='lease')
    assert error.value.code == 'lease_lost'
    with publisher.repository.session_factory() as session:
        row = session.scalar(select(DeliveryOutbox))
        assert row.status == 'pending' and row.attempt_count == 0 and row.error_code is None
        assert session.scalar(select(AutomationJob)).status == 'prepared'


@pytest.mark.parametrize('category', ['volume_breakout', 'market_radar', 'announcement_risk'])
def test_non_etf_target_weight_injection_never_becomes_public(formal, tmp_path, category):
    from tests.test_research_delivery_auxiliary import run, hotspot, theme, envelope
    from unittest.mock import Mock
    publisher, sub, source, _ = formal
    if category in ('market_radar', 'announcement_risk'):
        sub = publisher.service.update_subscription(sub['id'], dict(version=sub['version'], strategy_id=category))
        source = run(tmp_path, category, **(dict(hotspot_service=hotspot(leading_industries=[theme()]))
            if category == 'market_radar' else dict(announcement_fetcher=Mock(return_value=envelope()))))
    sentinel = 'PRIVATE_WEIGHTS_SENTINEL'
    source.result['target_weights'] = {'private_holdings': {'credential_ref': sentinel}}
    report = publisher.prepare(sub['id'], publisher.store_batch(source)['id'], lease_token='lease')
    assert report['detail']['research_weights'] == {}
    with publisher.repository.session_factory() as session:
        raw = json.dumps(report, default=str) + session.scalar(select(DeliveryOutbox)).payload_json
        assert all(v not in raw for v in (sentinel, 'private_holdings', 'credential_ref'))


def test_readable_chinese_report_preserves_order_units_and_material_risks(formal):
    publisher, _, source, _ = formal
    source.result['picks'][0].update(final_score=78.4991, screen_score=76.299112264, amount=301500338.58,
        total_mv=43968199422.00001, change_pct=2.0679, volume_ratio=3.61, turnover_rate=5.4956,
        breakout_20d_pct=.7289, volume_ratio_20d=4.6797, risk_score=0,
        risk_flags=['high_pb', '数据覆盖不足，未确认低风险：valuation, capital_flow, boards'])
    source.result['picks'].append(dict(source.result['picks'][0], code='600002', rank=2, risk_flags=['rsi_overbought']))
    source.coverage.update(independent_universe_count=5576, daily_response_count=5565, daily_basic_response_count=5565,
        daily_response_coverage=5565/5576, daily_basic_response_coverage=5565/5576, eligible_count=3927,
        unknown_missing_count=11, technical_planned_count=100, technical_not_evaluated_count=302)
    source.warnings.extend(['bounded_observed_bars_not_exchange_contiguous', 'concept_rankings_unavailable'])
    report = batch(formal)
    text = report['rendered_content']
    assert all(value in text for value in ('涨跌幅 +2.07%', '量比 3.61倍',
        '市净率偏高', 'RSI超买', '不保证交易日连续'))
    assert all(value not in text for value in ('必需字段校验', '发布窗口', '17:05', '22:30',
        '5565/5576', '99.8%', '3927', '缺失 11', '规则分 78.50', '概念排名数据缺失', '未确认低风险'))
    assert report['detail']['audit']['coverage']['eligible_count'] == 3927
    assert report['detail']['audit']['coverage']['daily_response_count'] == 5565
    assert report['envelope']['coverage']['technical_not_evaluated_count'] == 302
    assert text.index('600001') < text.index('600002')
    assert not any(value in text for value in ('daily_response_coverage=', 'scope=', 'risk_score=0',
        '43968199422.00001', '76.299112264', 'risk_penalty=', 'excluded_by_risk='))
    assert report['envelope']['candidates'][0]['metrics']['screen_score'] == 76.299112264


def test_announcement_index_is_not_claimed_as_primary_source(formal, tmp_path):
    from tests.test_research_delivery_auxiliary import run, announcement, envelope
    from unittest.mock import Mock
    publisher, sub, _, _ = formal
    sub = publisher.service.update_subscription(sub['id'], dict(version=sub['version'], strategy_id='announcement_risk'))
    source = run(tmp_path, 'announcement_risk', announcement_fetcher=Mock(return_value=envelope([
        announcement(source='EastmoneyAnnouncement', url='https://data.eastmoney.com/notices/detail/600001/AN1.html')
    ])))
    report = publisher.prepare(sub['id'], publisher.store_batch(source)['id'], lease_token='lease')
    text = report['rendered_content']
    assert all(value in text for value in ('近3日', '非风险排名', '来源链接'))
    assert '扫描 ' not in text and '展示 1/1' not in text
    assert report['detail']['audit']['coverage']['scanned_count'] == 1
    assert '一手' not in text and '评分不是胜率' not in text


def test_etf_public_weights_are_rebuilt_from_scalar_code_allowlist(formal):
    from src.services.research_delivery.render_formal import formal_envelope
    _, _, source, _ = formal
    data = source.to_dict()
    data['category_id'] = 'etf_rotation'
    data['result'] = dict(items=[dict(id='510300', name='ETF', metrics=dict(close=1., momentum=.012345),
        source='fixture', as_of=source.business_date)], target_weights={
            '510300': .6, '511880': .4, 'private_holdings': {'credential_ref': 'PRIVATE'},
            '600001': True, '600002': -1, '600003': 1.1, '600004': float('nan')})
    envelope, detail = formal_envelope(data, 5)
    assert detail['research_weights'] == {'510300': .6, '511880': .4}
    assert 'PRIVATE' not in json.dumps(dict(envelope=envelope, detail=detail))


def test_stale_owner_cannot_suppress_when_control_is_also_paused(formal):
    publisher, _, _, _ = formal
    report = batch(formal)
    with publisher.repository.atomic() as session:
        control = session.get(AutomationControl, 'main')
        control.lease_token, control.enabled = 'current', False
    with pytest.raises(ManagementError) as error:
        publisher.deliver(report['id'], lease_token='lease')
    assert error.value.code == 'lease_lost'
    with publisher.repository.session_factory() as session:
        assert session.scalar(select(DeliveryOutbox)).status == 'pending'
    assert publisher.deliver(report['id'], lease_token='current')['error_code'] == 'automation_paused'


def test_partial_common_assessment_gap_keeps_exact_candidate_scope(formal):
    _, _, source, _ = formal
    first = source.result['picks'][0]
    first['risk_flags'] = ['数据覆盖不足，未确认低风险：valuation']
    source.result['picks'].extend([
        dict(first, code='600002', rank=2),
        dict(first, code='600003', rank=3, risk_flags=['数据覆盖不足，未确认低风险：growth']),
    ])
    # Only one stock has a known disclosed growth observation. The important
    # strategy gap must be consolidated, without falsely attributing it to all.
    source.result['research_context'] = dict(version='research-context-v1', business_date=source.business_date,
        background={'600003': dict(issuer_code='600003', business_date=source.business_date,
            growth=dict(status='partial', source='tushare', data=dict(report_date='2026-06-30',
                announcement_date='2026-08-28', net_profit_yoy=1.)))})
    original = source.to_dict()
    report = batch(formal)
    text = report['rendered_content']
    assert '数据覆盖不足，未确认低风险' not in text
    assert '盈利变化（示例（600001）、示例（600002））' in text
    assert text.count('待核验：') == 1 and '机构披露' not in text and '龙虎榜' not in text
    assert [row['code'] for row in report['envelope']['candidates']] == ['600001', '600002', '600003']
    assert source.to_dict() == original
