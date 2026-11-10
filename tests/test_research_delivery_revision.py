"""Historical revisions use isolated SQLite and injected transport only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import uuid

import pytest
from sqlalchemy import select

from tests.test_research_delivery_aggregation import research
from tests.test_research_delivery_formal import formal, batch
from src.notification_sender.feishu_sender import DeliveryOutcome
from src.repositories.automation_models import AutomationControl, AutomationJob, PublicationRelease, ResearchBatch
from src.repositories.research_delivery_models import DeliveryDestination, DeliveryOutbox, ResearchPublication, DeliverySubscription
from src.repositories.research_delivery_repository import ManagementError
from src.services.research_delivery.revision_publisher import RevisionPublisher


@pytest.fixture
def revision(formal):
    original, sub, source, clock = formal
    report = batch(formal)
    original.sender = lambda *a: DeliveryOutcome('sent', None)
    original.deliver(report['id'], lease_token='lease')
    clock[0] += timedelta(days=1)
    publisher = RevisionPublisher(original.service, original.config, clock=lambda: clock[0])
    return publisher, report, str(uuid.uuid4()), clock


def snapshot(publisher, original_id):
    with publisher.repository.session_factory() as session:
        rows = [session.get(ResearchPublication, original_id), session.get(PublicationRelease, original_id),
            session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.publication_id == original_id)),
            session.scalar(select(AutomationJob)), session.scalar(select(ResearchBatch))]
        return [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows]


def prepared(revision):
    publisher, original, edition, _ = revision
    report = publisher.prepare(original['id'], edition_id=edition)
    manifest = publisher.inspect(edition)
    assert manifest['complete']
    return report, manifest['manifest_hash']


def test_revision_historical_reentry_single_send_and_original_integrity(revision):
    publisher, original, edition, _ = revision
    before = snapshot(publisher, original['id'])
    report, digest = prepared(revision)
    assert report['id'] == publisher.prepare(original['id'], edition_id=edition)['id']
    assert '修订版' in report['title'] and '2026-10-15' in report['title']
    assert report['source_run_id'] == original['source_run_id']  # screening run, not batch
    assert 'revision_binding' not in report
    calls = []
    def send(*args):
        with publisher.repository.session_factory() as session:
            row = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.publication_id == report['id']))
            assert row.status == 'sending' and row.attempt_count == 1
        calls.append(args)
        return DeliveryOutcome('sent', None)
    publisher.sender = send
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: publisher.deliver(report['id'], edition_id=edition, expected_manifest_hash=digest), range(2)))
    assert publisher.deliver(report['id'], edition_id=edition, expected_manifest_hash=digest)['status'] == 'sent'
    assert len(calls) == 1 and snapshot(publisher, original['id']) == before


@pytest.mark.parametrize('damage', ['not_sent', 'source', 'document', 'receipt_request', 'receipt_duplicate', 'batch', 'evidence'])
def test_original_chain_is_validated_before_model(revision, damage):
    publisher, original, edition, _ = revision
    with publisher.repository.atomic() as session:
        pub = session.get(ResearchPublication, original['id'])
        receipt = session.scalar(select(DeliveryOutbox))
        if damage == 'not_sent': receipt.status = 'pending'
        elif damage == 'source': pub.source_snapshot_json = '{}'
        elif damage == 'document': pub.document_json = '{}'
        elif damage == 'receipt_request': receipt.request_id = 'formal:wrong'
        elif damage == 'receipt_duplicate':
            values = {c.name: getattr(receipt, c.name) for c in receipt.__table__.columns}
            values.update(id=uuid.uuid4().hex, request_id='duplicate')
            session.add(DeliveryOutbox(**values))
        elif damage == 'batch': session.scalar(select(PublicationRelease)).batch_id = 'wrong'
        else:
            from pathlib import Path
            Path(json.loads(pub.source_snapshot_json)['evidence'][0]['path']).unlink()
    publisher.explanations.explain = lambda *a: pytest.fail('invalid original must not call model')
    with pytest.raises((ManagementError, ValueError, OSError)):
        publisher.prepare(original['id'], edition_id=edition)


@pytest.mark.parametrize('damage', ['epoch', 'config', 'credentials', 'original', 'payload', 'expiry', 'manifest'])
def test_delivery_revalidates_complete_manifest_and_configuration(revision, damage):
    publisher, original, edition, clock = revision
    report, digest = prepared(revision)
    with publisher.repository.atomic() as session:
        if damage == 'epoch': session.get(AutomationControl, 'main').version += 1
        elif damage == 'config': session.get(DeliverySubscription, original['subscription_id']).top_n += 1
        elif damage == 'credentials': session.get(DeliveryDestination, original['destination_id']).credential_ref = uuid.uuid4().hex
        elif damage == 'original': session.get(ResearchPublication, original['id']).content_hash = 'changed'
        elif damage == 'payload': session.get(PublicationRelease, report['id']).payload_json = '{}'
        elif damage == 'expiry': clock[0] += timedelta(minutes=61)
        else: digest = 'wrong'
    publisher.sender = lambda *a: pytest.fail('invalid manifest must never send')
    with pytest.raises((ManagementError, ValueError)):
        publisher.deliver(report['id'], edition_id=edition, expected_manifest_hash=digest)


def test_unknown_never_retries_and_inspect_is_readonly(revision):
    publisher, original, edition, _ = revision
    report, digest = prepared(revision)
    calls = []
    publisher.sender = lambda *a: calls.append(a) or DeliveryOutcome('delivery_unknown', 'timeout')
    assert publisher.deliver(report['id'], edition_id=edition, expected_manifest_hash=digest)['status'] == 'delivery_unknown'
    assert publisher.deliver(report['id'], edition_id=edition, expected_manifest_hash=digest)['status'] == 'delivery_unknown'
    before = snapshot(publisher, original['id'])
    publisher.inspect(edition)
    assert snapshot(publisher, original['id']) == before and len(calls) == 1


@pytest.mark.parametrize('override,day,limit', [('2026-10-16:40', '2026-10-16', 40),
    ('2026-10-16:40', '2026-10-17', 20), ('2026-10-16:41', '2026-10-16', 20),
    ('20261016:40', '2026-10-16', 20), ('2026-02-30:40', '2026-02-30', 20), ('', '2026-10-16', 20)])
def test_date_bound_budget_and_cached_reuse(research, monkeypatch, override, day, limit):
    repository = research[0].repository
    monkeypatch.setenv('RESEARCH_DELIVERY_LLM_BUDGET_OVERRIDE', override)
    for i in range(limit):
        assert repository.reserve_llm(str(i), day)['created']
    assert repository.reserve_llm('exhausted', day) is None
    assert repository.reserve_llm('0', day)['created'] is False
    attempt = repository.reserve_llm('0', day)
    cached = dict(status='succeeded', explanation=None)
    repository.finish_llm(attempt['id'], cached)
    monkeypatch.setenv('RESEARCH_DELIVERY_LLM_BUDGET_OVERRIDE', '')
    assert repository.reserve_llm('0', day)['result'] == cached


def second_original(revision):
    from src.services.research_delivery.formal_publisher import FormalPublisher
    publisher, original, _, clock = revision
    sub = publisher.service.create_subscription(dict(name='second', destination_id=original['destination_id'],
        strategy_id=original['envelope']['strategy_id'], llm_mode='off'))
    sub = publisher.service.update_subscription(sub['id'], dict(version=sub['version'], enabled=True))
    formal = FormalPublisher(publisher.service, publisher.config, clock=lambda: clock[0] - timedelta(days=1),
        sender=lambda *a: DeliveryOutcome('sent', None))
    with publisher.repository.session_factory() as session:
        batch_id = session.get(PublicationRelease, original['id']).batch_id
    report = formal.prepare(sub['id'], batch_id, lease_token='lease')
    formal.deliver(report['id'], lease_token='lease')
    return report


def test_complete_edition_scope_is_frozen_before_each_model_call(revision):
    publisher, original, edition, _ = revision
    second = second_original(revision)
    first = publisher.prepare(original['id'], edition_id=edition)
    partial = publisher.inspect(edition)
    assert partial['expected_count'] == 2 and not partial['complete'] and partial['manifest_hash'] is None
    publisher.sender = lambda *a: pytest.fail('incomplete edition cannot send')
    with pytest.raises(ManagementError):
        publisher.deliver(first['id'], edition_id=edition, expected_manifest_hash='anything')
    second = publisher.prepare(second['id'], edition_id=edition)
    manifest = publisher.inspect(edition)
    assert manifest['complete'] and len(manifest['members']) == 2
    with publisher.repository.atomic() as session:
        session.delete(session.get(ResearchPublication, second['id']))
    with pytest.raises(ManagementError):
        publisher.deliver(first['id'], edition_id=edition, expected_manifest_hash=manifest['manifest_hash'])


def test_same_edition_scope_change_is_rejected_before_model(revision):
    publisher, original, edition, _ = revision
    publisher.prepare(original['id'], edition_id=edition)
    second = second_original(revision)
    publisher.explanations.explain = lambda *a: pytest.fail('scope change must not call model')
    with pytest.raises(ManagementError):
        publisher.prepare(second['id'], edition_id=edition)


def test_same_manifest_sends_whole_edition_sequentially_and_remains_stable(revision):
    publisher, original, edition, _ = revision
    second = second_original(revision)
    before = snapshot(publisher, original['id'])
    publisher.prepare(original['id'], edition_id=edition)
    publisher.prepare(second['id'], edition_id=edition)
    manifest = publisher.inspect(edition)
    calls = []
    publisher.sender = lambda *a: calls.append(a) or DeliveryOutcome('sent', None)
    for _ in range(2):
        for member in manifest['members']:
            assert publisher.deliver(member['publication_id'], edition_id=edition,
                expected_manifest_hash=manifest['manifest_hash'])['status'] == 'sent'
        assert publisher.inspect(edition)['manifest_hash'] == manifest['manifest_hash']
    assert len(calls) == 2 and snapshot(publisher, original['id']) == before


def test_same_identity_configuration_and_source_conflicts_precede_cache(revision):
    publisher, original, edition, _ = revision
    publisher.prepare(original['id'], edition_id=edition)
    publisher.explanations.explain = lambda *a: pytest.fail('conflict must not call model')
    with publisher.repository.atomic() as session:
        session.get(DeliverySubscription, original['subscription_id']).llm_mode = 'explain_only'
    with pytest.raises(ManagementError):
        publisher.prepare(original['id'], edition_id=edition)


def test_inprogress_model_does_not_freeze_temporary_fallback(revision):
    publisher, original, edition, _ = revision
    publisher.explanations.explain = lambda *a: dict(status='fallback', reason='attempt_in_progress_or_unknown', explanation=None)
    first = publisher.prepare(original['id'], edition_id=edition)
    assert first['status'] == 'preparing' and publisher.inspect(edition)['complete'] is False
    with publisher.repository.session_factory() as session:
        assert session.get(PublicationRelease, first['id']) is None
    publisher.explanations.explain = lambda *a: dict(status='off', explanation=None)
    assert publisher.prepare(original['id'], edition_id=edition)['id'] == first['id']
    assert publisher.inspect(edition)['complete']


def test_configuration_is_checked_after_model_before_release(revision):
    publisher, original, edition, _ = revision
    def explain(*args):
        with publisher.repository.atomic() as session:
            session.get(AutomationControl, 'main').version += 1
        return dict(status='off', explanation=None)
    publisher.explanations.explain = explain
    with pytest.raises(ManagementError):
        publisher.prepare(original['id'], edition_id=edition)
    with publisher.repository.session_factory() as session:
        assert session.query(PublicationRelease).count() == 1


def test_abandoned_sending_inspect_does_not_recover_and_deliver_never_retries(revision):
    publisher, original, edition, clock = revision
    report, digest = prepared(revision)
    with publisher.repository.atomic() as session:
        row = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.publication_id == report['id']))
        row.status, row.attempt_count = 'sending', 1
        row.sending_at = publisher._utc(clock[0]) - timedelta(minutes=2)
    assert publisher.inspect(edition)['outcomes'][0]['status'] == 'sending'
    publisher.sender = lambda *a: pytest.fail('unknown cannot retry')
    result = publisher.deliver(report['id'], edition_id=edition, expected_manifest_hash=digest)
    assert result['status'] == 'delivery_unknown' and result['error_code'] == 'abandoned_sending'


def test_existing_only_cli_repository_readonly_and_no_schema_initializer(revision, tmp_path, monkeypatch):
    from scripts.revise_research_delivery import existing_repository, main
    publisher, original, edition, _ = revision
    report, _ = prepared(revision)
    with publisher.repository.session_factory() as session:
        db_path = session.get_bind().url.database
    missing = tmp_path / 'typo.db'
    with pytest.raises(FileNotFoundError):
        existing_repository(missing, readonly=False)
    assert not missing.exists()
    monkeypatch.setattr(type(publisher.repository), '__init__', lambda *a: pytest.fail('initializer forbidden'))
    monkeypatch.setattr('src.config.get_config', lambda: pytest.fail('inspect must not load env'))
    before = snapshot(publisher, original['id'])
    assert main(['inspect', '--db', db_path, '--vault', str(tmp_path / 'unused-vault'),
        '--day', '2026-10-15', '--edition', edition, '--output-dir', str(tmp_path / 'review')]) == 0
    assert snapshot(publisher, original['id']) == before
    assert not (tmp_path / 'unused-vault').exists()
    assert 'credential_ref' not in (tmp_path / 'review' / 'manifest.json').read_text(encoding='utf-8')
    assert 'original_source_json' not in (tmp_path / 'review' / 'reports.html').read_text(encoding='utf-8')


def test_reserved_request_collision_is_checked_before_model(revision):
    publisher, original, edition, _ = revision
    with publisher.repository.atomic() as session:
        original_row = session.scalar(select(DeliveryOutbox))
        values = {c.name: getattr(original_row, c.name) for c in original_row.__table__.columns}
        values.update(id=uuid.uuid4().hex, request_id=publisher.request_id(edition, original['subscription_id']),
            kind='test', publication_id=None)
        session.add(DeliveryOutbox(**values))
    publisher.explanations.explain = lambda *a: pytest.fail('namespace collision must not call model')
    with pytest.raises(ManagementError):
        publisher.prepare(original['id'], edition_id=edition)


def test_concurrent_prepares_do_not_replace_model_result_with_inprogress_fallback(revision):
    from threading import Barrier, Event
    from src.services.research_delivery.explanation import ExplanationService
    from tests.test_research_delivery_explanation import model_config
    from tests.test_research_delivery_signals import selection
    publisher, original, edition, _ = revision
    with publisher.repository.atomic() as session:
        session.get(DeliverySubscription, original['subscription_id']).llm_mode = 'explain_only'
    calls, gate, done = [], Barrier(2), Event()
    def complete(prompt, *args, **kwargs):
        calls.append(prompt)
        done.wait(timeout=5)
        projected = json.loads(prompt.removeprefix('<frozen_input>').removesuffix('</frozen_input>'))
        return json.dumps(selection(projected)), 'openai/vendor/model', {}
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=complete)
    explain = publisher.explanations.explain
    def synchronized(*args):
        gate.wait(timeout=5)
        result = explain(*args)
        if result.get('reason') == 'attempt_in_progress_or_unknown':
            done.set()
        return result
    publisher.explanations.explain = synchronized
    with ThreadPoolExecutor(2) as pool:
        reports = list(pool.map(lambda _: publisher.prepare(original['id'], edition_id=edition), range(2)))
    assert len(calls) == 1 and len({report['id'] for report in reports}) == 1
    assert publisher.inspect(edition)['complete']
    with publisher.repository.session_factory() as session:
        final = session.get(ResearchPublication, reports[0]['id'])
        assert json.loads(final.document_json)['llm']['status'] == 'succeeded'


def test_expiry_during_credential_resolution_stops_before_claim(revision, monkeypatch):
    publisher, _, edition, clock = revision
    report, digest = prepared(revision)
    resolve = publisher.service.vault.resolve
    def delayed(reference):
        credentials = resolve(reference)
        clock[0] += timedelta(minutes=61)
        return credentials
    monkeypatch.setattr(publisher.service.vault, 'resolve', delayed)
    publisher.sender = lambda *a: pytest.fail('expired final claim cannot send')
    with pytest.raises(ManagementError):
        publisher.deliver(report['id'], edition_id=edition, expected_manifest_hash=digest)
    with publisher.repository.session_factory() as session:
        row = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.publication_id == report['id']))
        assert row.status == 'pending' and row.attempt_count == 0
