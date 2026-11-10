"""Backtest delivery uses temporary artifacts, databases and fake transport only."""
import csv
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from data_provider.historical_reference import digest
from src.services.screening.backtest import freeze_config, write_json
from src.repositories.research_delivery_models import DeliveryDestination, DeliverySubscription, DeliveryOutbox
from src.services.research_delivery.credentials import Credentials, credential_fingerprint
from src.notification_sender.feishu_sender import DeliveryOutcome, send_once
from src.services.research_delivery.backtest_delivery import BacktestDelivery


def seal(path):
    config = json.loads((path / 'config.json').read_text('utf-8'))
    write_json(path / 'manifest.json', dict(config_hash=digest(config), dataset_hash='a' * 64,
        reference_hash='b' * 64, semantics=config['semantics'], files={
        name: sha256((path / name).read_bytes()).hexdigest() for name in
        ('config.json', 'metrics.json', 'data_quality.json', 'signals.csv', 'trades.csv', 'report.md')}))


@pytest.fixture
def artifacts(tmp_path):
    path = tmp_path / 'artifacts'
    config, _ = freeze_config(dict(output_dir=str(path)))
    cells = []
    for strategy in config['strategies']:
        for start in config['start_dates']:
            for months in config['window_months']:
                for holding in config['holding_sessions']:
                    complete = start == '2026-01-05'
                    cells.append(dict(strategy=strategy, start=start, months=months, holding_sessions=holding,
                        status='no-signal' if complete else 'incomplete', reason=None,
                        signal_window_complete=complete, scheduled_cohorts=1, valid_cohorts=int(complete),
                        mature_cohorts=int(complete), blocked_slots=0, unresolved_slots=0,
                        **{key: 0. if complete else None for key in ('mean_net_return', 'median_net_return',
                        'positive_cohort_proportion', 'worst_cohort', 'mean_double_friction_return', 'mean_benchmark_difference')}))
    write_json(path / 'metrics.json', dict(cells=cells, planned_cells=320, completed_cells=80))
    write_json(path / 'data_quality.json', {'2026-01-05': dict(status='valid'), '_planned_calendar_errors': {}})
    for name, fields in [('signals.csv', ['strategy', 'signal_date', 'code', 'industry_code', 'industry_taxonomy']),
                         ('trades.csv', ['strategy', 'signal_date', 'code', 'holding_sessions', 'status', 'reason',
                          'entry_date', 'exit_date', 'net_return', 'double_friction_return', 'benchmark_return'])]:
        with (path / name).open('w', newline='', encoding='utf-8-sig') as stream:
            csv.writer(stream).writerow(fields)
    (path / 'report.md').write_text('retrospective replay', encoding='utf-8')
    seal(path)
    return path


@pytest.mark.parametrize('workers', [1, 3, 4, 5, 6, 0, -1, 7, True, 1.5, '6'])
def test_execution_worker_field_bound(artifacts, workers):
    from src.services.research_delivery.backtest_delivery import validate_artifacts
    config = json.loads((artifacts / 'config.json').read_text(encoding='utf-8'))
    config['execution_workers'] = workers
    write_json(artifacts / 'config.json', config)
    seal(artifacts)
    if type(workers) is int and 1 <= workers <= 6:
        validate_artifacts(artifacts)
    else:
        with pytest.raises(ValueError, match='execution_workers_invalid'):
            validate_artifacts(artifacts)


@pytest.fixture
def delivery(tmp_path, artifacts):
    engine = create_engine('sqlite:///' + str(tmp_path / 'delivery.sqlite'))
    for model in (DeliveryDestination, DeliverySubscription, DeliveryOutbox):
        model.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    credentials = Credentials('https://open.feishu.cn/open-apis/bot/v2/hook/fake')
    with sessions.begin() as session:
        session.add(DeliveryDestination(id='dest', alias='稳健投资', kind='strategy', enabled=True, version=1,
            credential_ref='c' * 32, credential_fingerprint=credential_fingerprint(credentials.webhook_url)))
        session.add(DeliverySubscription(id='sub', name='稳健投资', strategy_id='quality_value',
            destination_id='dest', top_n=5, llm_mode='off', planned_time='18:00', enabled=True, version=1))
        session.add(DeliverySubscription(id='radar', name='雷达', strategy_id='radar',
            destination_id='radar-dest', top_n=5, llm_mode='off', planned_time='18:00', enabled=True, version=1))
    calls = []
    def sender(credentials, payload):
        with sessions() as session:
            row = session.scalar(select(DeliveryOutbox))
            assert row.status == 'delivery_unknown' and row.attempt_count == 1
        calls.append(payload)
        return DeliveryOutcome('sent')
    service = BacktestDelivery(SimpleNamespace(session_factory=sessions),
        SimpleNamespace(resolve=lambda ref: credentials), sender=sender)
    yield service, sessions, calls
    engine.dispose()


def test_preview_readonly_and_cash_language(delivery, artifacts):
    service, sessions, calls = delivery
    preview = service.preview(artifacts)
    assert len(preview['cards']) == 1
    card = preview['cards'][0]
    assert card['strategy_id'] == 'quality_value' and card['destination_id'] == 'dest'
    assert '没有可执行信号' in json.dumps(card, ensure_ascii=False)
    assert '8/32' in json.dumps(card, ensure_ascii=False)
    with sessions() as session:
        assert session.scalar(select(DeliveryOutbox)) is None
    assert calls == []


def test_mobile_summary_preserves_each_strategy_scope_and_costs(artifacts):
    from src.services.research_delivery.backtest_delivery import summarize, validate_artifacts
    artifact = validate_artifacts(artifacts)
    assert len(artifact['config']['strategies']) == 10
    for strategy, config in artifact['config']['strategies'].items():
        content = summarize(artifact, strategy)
        assert content.startswith(f"{config['display_name']} v{config['version']}")
        assert '完成 8/32格 · 待成熟 24格' in content
        assert '2026-01-05、04-01、07-01、09-01' in content and '各1/3个月' in content
        assert '买入0.08%、卖出0.13%' in content and '假设费用' in content
        assert 'D+1开盘' in content and '各20%' in content and '留现金' in content
        assert '成交0次，现金收益0' in content
        for holding in (3, 5, 10, 20):
            assert f'持有{holding}个交易日 · 完整2格' in content
        assert content.count('相对沪深300') == 4
        assert content.count('可比/完整 2/2') == 4
        assert content.count('双成本') == 4
        assert not any(term in content for term in (' | ', '已验收信号窗口', '保留全部原件', 'cohort', 'alpha', 'NAV', '不调权重', '择优窗口'))
        assert content.endswith('当前规则历史回放；窗口有重叠，基准为沪深300价格指数。')


@pytest.mark.parametrize('outcome', ['sent', 'rejected', 'delivery_unknown', 'exception'])
def test_once_only_receipt_all_outcomes(delivery, artifacts, outcome):
    service, sessions, calls = delivery
    if outcome != 'sent':
        def sender(credentials, payload):
            calls.append(payload)
            if outcome == 'exception':
                raise TimeoutError('must not leak')
            return DeliveryOutcome(outcome)
        service.sender = sender
    preview = service.preview(artifacts)
    result = service.send(artifacts, preview['review_hash'])
    assert result[0]['status'] == ('delivery_unknown' if outcome == 'exception' else outcome)
    service.send(artifacts, preview['review_hash'])
    assert len(calls) == 1


def test_disabled_and_changed_route(delivery, artifacts):
    service, sessions, calls = delivery
    preview = service.preview(artifacts)
    with sessions.begin() as session:
        session.get(DeliveryDestination, 'dest').enabled = False
    assert not service.preview(artifacts)['cards']
    with pytest.raises(ValueError, match='review_changed'):
        service.send(artifacts, preview['review_hash'])
    assert not calls


@pytest.mark.parametrize('failure', ['hash', 'missing', 'matrix', 'strategy', 'history', 'metric'])
def test_invalid_or_missing_artifact_blocks(delivery, artifacts, failure):
    service, _, calls = delivery
    if failure == 'missing':
        (artifacts / 'trades.csv').unlink()
    elif failure == 'hash':
        (artifacts / 'report.md').write_text('changed', encoding='utf-8')
    else:
        name = 'config.json' if failure == 'strategy' else 'metrics.json'
        value = json.loads((artifacts / name).read_text('utf-8'))
        if failure == 'strategy':
            value['strategies']['quality_value']['version'] = 'old'
        elif failure == 'matrix':
            value['cells'].pop()
        elif failure == 'history':
            value['cells'][0]['status'] = 'unavailable'
            value['cells'][0]['reason'] = 'planned_calendar_unavailable'
        else:
            value['cells'][0]['mean_net_return'] = .5
        write_json(artifacts / name, value)
        seal(artifacts)
    preview = service.preview(artifacts)
    assert preview['blockers'] and not preview['cards']
    assert not calls


def test_duplicate_strategy_mapping_is_not_guessed(delivery, artifacts):
    service, sessions, _ = delivery
    with sessions.begin() as session:
        session.add(DeliverySubscription(id='dup', name='稳健投资', strategy_id='quality_value',
            destination_id='dest', top_n=5, llm_mode='off', planned_time='18:00', enabled=True, version=1))
    assert service.preview(artifacts)['blockers'] == ['route_ambiguous:quality_value']


def test_credentials_hash_rechecked(delivery, artifacts):
    service, _, calls = delivery
    preview = service.preview(artifacts)
    service.vault = SimpleNamespace(resolve=lambda ref: Credentials('https://open.feishu.cn/open-apis/bot/v2/hook/other'))
    assert service.send(artifacts, preview['review_hash'])[0]['status'] == 'rejected'
    assert not calls


@pytest.mark.parametrize('change', ['subscription', 'destination', 'credential', 'kind', 'shared_radar'])
def test_route_changes_invalidate_review(delivery, artifacts, change):
    service, sessions, calls = delivery
    preview = service.preview(artifacts)
    with sessions.begin() as session:
        if change == 'subscription':
            session.get(DeliverySubscription, 'sub').version += 1
        elif change == 'destination':
            session.get(DeliveryDestination, 'dest').version += 1
        elif change == 'credential':
            session.get(DeliveryDestination, 'dest').credential_ref = 'd' * 32
        elif change == 'kind':
            session.get(DeliveryDestination, 'dest').kind = 'radar'
        else:
            session.get(DeliverySubscription, 'radar').destination_id = 'dest'
    with pytest.raises(ValueError, match='review_changed'):
        service.send(artifacts, preview['review_hash'])
    assert not calls


@pytest.mark.parametrize('provider_code,status', [(0, 'sent'), (19001, 'rejected'), (None, 'delivery_unknown')])
def test_real_single_sender_with_fake_http(delivery, artifacts, provider_code, status):
    service, sessions, _ = delivery
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        assert kwargs['allow_redirects'] is False
        if provider_code is None:
            raise TimeoutError()
        return SimpleNamespace(status_code=200, json=lambda: {'code': provider_code})
    service.sender = lambda credentials, payload: send_once(credentials, payload, transport=post)
    review_hash = service.preview(artifacts)['review_hash']
    assert service.send(artifacts, review_hash)[0]['status'] == status
    service.send(artifacts, review_hash)
    assert len(calls) == 1
    with sessions() as session:
        row = session.scalar(select(DeliveryOutbox))
        assert row.kind == 'backtest' and row.publication_id is None
        assert 'https://' not in row.payload_json and 'fake' not in row.payload_json


def test_cli_preview_no_vault_or_send(delivery, artifacts, monkeypatch, capsys):
    from scripts import send_screening_backtests as cli
    service, _, calls = delivery
    engine = SimpleNamespace(dispose=Mock())
    opened = []
    def existing(path, *, readonly):
        opened.append(readonly)
        return service.repository, engine
    monkeypatch.setattr(cli, 'existing_repository', existing)
    monkeypatch.setattr(cli, 'CredentialVault', Mock(side_effect=AssertionError('preview must not open vault')))
    assert cli.main(['--artifact-dir', str(artifacts), '--db', 'unused']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['review_hash'] and len(result['cards']) == 1
    assert opened == [True] and not calls
    assert 'credential_ref' not in json.dumps(result)
    engine.dispose.assert_called_once()


@pytest.fixture
def actual_artifacts(tmp_path):
    from datetime import date
    import pandas as pd
    from src.services.screening.backtest import run_backtest
    from src.services.screening.backtest_data import BatchStore
    from tests.test_screening_backtest_data import bundle
    class EmptyDataset:
        fingerprint = 'a' * 64
        last_available_date = date(2026, 10, 16)
        references = bundle()
        store = BatchStore(tmp_path / 'empty.sqlite')
        def sessions(self, exchange, start, end):
            calendar, _ = self.references.calendar(exchange, start, end)
            return sorted(d for d, opened in calendar.items() if opened)
        def snapshot(self, signal):
            return pd.DataFrame(), dict(universe_count=1, exclusions=[dict(code='600001', reason='verified_ST')])
    dataset = EmptyDataset()
    path = tmp_path / 'actual-run'
    try:
        run_backtest(dict(output_dir=str(path)), dataset)
        yield path
    finally:
        dataset.store.close()


def test_completed_artifacts_from_actual_task3_writer(delivery, actual_artifacts):
    service, _, _ = delivery
    preview = service.preview(actual_artifacts)
    assert not preview['blockers'] and len(preview['cards']) == 1
    assert '28/32' in json.dumps(preview, ensure_ascii=False)


def test_cli_real_repository_preview_and_once_only_send(delivery, actual_artifacts, tmp_path, monkeypatch, capsys):
    from scripts import send_screening_backtests as cli
    service, sessions, calls = delivery
    db = tmp_path / 'delivery.sqlite'
    before = db.read_bytes()
    monkeypatch.setattr(cli, 'CredentialVault', Mock(side_effect=AssertionError('preview opened vault')))
    monkeypatch.setattr(cli.ResearchDeliveryRepository, '__init__', Mock(side_effect=AssertionError('schema initializer')))
    argv = ['--artifact-dir', str(actual_artifacts), '--db', str(db)]
    assert cli.main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert db.read_bytes() == before and not calls
    repository, engine = cli.existing_repository(db, readonly=True)
    try:
        with repository.session_factory() as session:
            from sqlalchemy import text
            with pytest.raises(Exception, match='readonly'):
                session.execute(text('DELETE FROM research_delivery_outbox'))
    finally:
        engine.dispose()
    monkeypatch.setattr(cli, 'CredentialVault', lambda path: service.vault)
    real_delivery = cli.BacktestDelivery
    monkeypatch.setattr(cli, 'BacktestDelivery', lambda repository, vault: real_delivery(repository, vault, sender=service.sender))
    send_args = argv + ['--send', '--vault', str(tmp_path / 'fake-vault'), '--expected-review-hash', preview['review_hash']]
    assert cli.main(send_args) == 0
    assert json.loads(capsys.readouterr().out)[0]['status'] == 'sent'
    assert cli.main(send_args) == 0
    capsys.readouterr()
    assert len(calls) == 1
    with sessions() as session:
        row = session.scalar(select(DeliveryOutbox))
        assert row.status == 'sent' and row.attempt_count == 1


def test_offline_guard_uri_boundary(tmp_path):
    from pathlib import Path
    import tempfile
    guard = Path(__file__).resolve().parents[1] / '.superpowers/sdd/2026-10-17-strategy-integrity-and-backtest/task-2-offline-tests.py'
    namespace = {'__file__': str(guard)}
    exec(compile(guard.read_text('utf-8').split('with ExitStack() as stack:')[0], str(guard), 'exec'), namespace)
    connect = Mock(return_value='connected')
    namespace['real_connect'] = connect
    allowed = (tmp_path / 'uri space.sqlite').as_uri() + '?mode=ro'
    assert namespace['database'](allowed, uri=True) == 'connected'
    connect.assert_called_once_with(allowed, uri=True)
    connect.reset_mock()
    outside = Path(tempfile.gettempdir()).resolve().parent / 'production.sqlite'
    production = guard.parents[3] / 'data/stock_analysis.db'
    for uri in [outside.as_uri() + '?mode=ro', production.as_uri() + '?mode=ro', 'file://remote/share/db?mode=ro',
                (Path(tempfile.gettempdir()).resolve().as_uri() + '/../production.sqlite?mode=ro'),
                (Path(tempfile.gettempdir()).resolve().as_uri() + '/%2e%2e/production.sqlite?mode=ro'),
                'file:///C:/invalid%ZZ.sqlite?mode=ro', allowed + '&vfs=other']:
        with pytest.raises((AssertionError, ValueError)):
            namespace['database'](uri, uri=True)
    connect.assert_not_called()


@pytest.mark.parametrize('blocked', [False, True])
def test_executed_and_known_blocked_cells(delivery, artifacts, blocked):
    strategy = 'quality_value'
    with (artifacts / 'signals.csv').open('a', newline='', encoding='utf-8-sig') as stream:
        csv.writer(stream).writerow([strategy, '2026-01-05', '600001', 'J66', 'capco_major_v1'])
    with (artifacts / 'trades.csv').open('a', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        for h in (3, 5, 10, 20):
            writer.writerow([strategy, '2026-01-05', '600001', h, 'blocked_entry_cash' if blocked else 'filled',
                'suspended_or_upper_limit_open' if blocked else '', '2026-01-06', '' if blocked else '2026-02-04',
                0 if blocked else .1, 0 if blocked else .09, 0 if blocked else .05])
    metrics = json.loads((artifacts / 'metrics.json').read_text('utf-8'))
    metrics['category'] = 'negative in completed windows'  # Must not reuse a global category.
    for cell in metrics['cells']:
        if cell['strategy'] == strategy and cell['status'] == 'no-signal':
            cell.update(status='completed', blocked_slots=int(blocked), mean_net_return=0 if blocked else .02,
                median_net_return=0 if blocked else .02, positive_cohort_proportion=0 if blocked else 1,
                worst_cohort=0 if blocked else .02, mean_double_friction_return=0 if blocked else .018,
                mean_benchmark_difference=0 if blocked else .01)
    write_json(artifacts / 'metrics.json', metrics)
    seal(artifacts)
    service, _, _ = delivery
    preview = service.preview(artifacts)
    assert not preview['blockers']
    content = json.dumps(preview, ensure_ascii=False)
    assert ('没有可执行信号' if blocked else '完整窗口均正') in content
    assert ('买入受阻8次' if blocked else '成交8次') in content
    assert '完整窗口均负' not in content


def test_missing_prerequisite_has_precise_local_reason(delivery, artifacts):
    quality = json.loads((artifacts / 'data_quality.json').read_text('utf-8'))
    quality['2026-07-02'] = dict(status='unavailable', reason='capco_publication_gap')
    write_json(artifacts / 'data_quality.json', quality)
    seal(artifacts)
    service, _, calls = delivery
    preview = service.preview(artifacts)
    assert preview['blockers'] == ['historical_gap:quality_value:capco_publication_gap']
    assert not preview['cards'] and not calls


@pytest.mark.parametrize('unknown_net', [-.3, .2])
def test_partial_benchmark_never_labels_subset_as_all_windows(delivery, artifacts, unknown_net):
    strategy = 'quality_value'
    # January belongs to both fixed windows; February only to the 3-month one.
    # Thus every H has one comparable complete cell and one unknown comparison.
    quality = json.loads((artifacts / 'data_quality.json').read_text('utf-8'))
    quality['2026-02-05'] = dict(status='valid')
    write_json(artifacts / 'data_quality.json', quality)
    with (artifacts / 'signals.csv').open('a', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        for signal in ('2026-01-05', '2026-02-05'):
            writer.writerow([strategy, signal, '600001', 'J66', 'capco_major_v1'])
    with (artifacts / 'trades.csv').open('a', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        for holding in (3, 5, 10, 20):
            writer.writerow([strategy, '2026-01-05', '600001', holding, 'filled', '',
                '2026-01-06', '2026-02-04', .1, .09, .05])
            writer.writerow([strategy, '2026-02-05', '600001', holding, 'filled', '',
                '2026-02-06', '2026-03-06', unknown_net, unknown_net - .01, ''])
    metrics = json.loads((artifacts / 'metrics.json').read_text('utf-8'))
    for cell in metrics['cells']:
        if cell['start'] != '2026-01-05':
            continue
        if cell['months'] == 3:
            cell.update(scheduled_cohorts=2, valid_cohorts=2, mature_cohorts=2)
        if cell['strategy'] != strategy:
            continue
        net = .02 if cell['months'] == 1 else (.1 + unknown_net) / 10
        cell.update(status='completed', mean_net_return=net, median_net_return=net,
            positive_cohort_proportion=1 if cell['months'] == 1 or unknown_net > 0 else .5,
            worst_cohort=.02 if cell['months'] == 1 else min(.02, unknown_net / 5),
            mean_double_friction_return=.018 if cell['months'] == 1 else (.08 + unknown_net) / 10,
            mean_benchmark_difference=.01 if cell['months'] == 1 else None)
    write_json(artifacts / 'metrics.json', metrics)
    seal(artifacts)
    service, _, calls = delivery
    preview = service.preview(artifacts)
    assert not preview['blockers'] and len(preview['cards']) == 1
    content = json.dumps(preview, ensure_ascii=False)
    assert '可比较证据不足' in content
    assert '完整窗口均正' not in content and '完整窗口均负' not in content
    assert content.count('相对沪深300 1.00%～1.00%（可比/完整 1/2；1格缺基准）') == 4
    assert '-2.00%' in content if unknown_net < 0 else '3.00%' in content
    assert sum(c['mean_benchmark_difference'] is None for c in metrics['cells']
        if c['strategy'] == strategy and c['status'] == 'completed') == 4
    assert not calls


def test_crash_after_durable_claim_never_retries(delivery, artifacts):
    service, sessions, calls = delivery
    def crash(*args):
        raise KeyboardInterrupt()
    service.sender = crash
    review_hash = service.preview(artifacts)['review_hash']
    with pytest.raises(KeyboardInterrupt):
        service.send(artifacts, review_hash)
    with sessions() as session:
        row = session.scalar(select(DeliveryOutbox))
        assert row.status == 'delivery_unknown' and row.error_code == 'claimed_before_transport'
    assert service.send(artifacts, review_hash)[0]['status'] == 'delivery_unknown'
    assert not calls


def test_backtest_kind_public_list_and_recovery_compatible(delivery, artifacts):
    from api.v1.endpoints.research_delivery import deliveries, delivery as delivery_endpoint
    from fastapi.encoders import jsonable_encoder
    from src.repositories.research_delivery_repository import ResearchDeliveryRepository
    service, sessions, _ = delivery
    service.sender = lambda *_: DeliveryOutcome('delivery_unknown')
    review_hash = service.preview(artifacts)['review_hash']
    service.send(artifacts, review_hash)
    repository = object.__new__(ResearchDeliveryRepository)
    repository.session_factory = sessions
    publisher = SimpleNamespace(repository=repository)
    listed = jsonable_encoder(deliveries(publisher))['items']
    assert len(listed) == 1 and listed[0]['kind'] == 'backtest'
    assert listed[0]['status'] == 'delivery_unknown' and listed[0]['attempt_count'] == 1
    single = jsonable_encoder(delivery_endpoint(listed[0]['id'], publisher))
    assert single == listed[0]
    # Existing public recovery handles only pending/sending, not these durable
    # claimed unknown receipts, and public rows never expose payload or vault.
    assert 'credential_ref' not in single and 'payload_json' not in single
    assert service.send(artifacts, review_hash)[0]['status'] == 'delivery_unknown'
