"""Saved-input CLI regression fixtures: never use the real database or providers."""
from copy import deepcopy
import json
import sqlite3
from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

import pytest

from scripts.preview_research_brief import LedgerOnly, main, replay, read_batches, saved_context
from src.services.research_delivery.aggregation import canonical, content_hash


@pytest.fixture
def saved(tmp_path):
    source = dict(category_id='volume_breakout', business_date='2026-10-12', state='ready',
        strategy_version='v1', coverage={}, warnings=[], blockers=[], evidence=[],
        result=dict(ranking_mode='factor', picks=[dict(code='600001', name='', rank=1,
            price=10., volume_ratio=3., breakout_20d_pct=1.2, risk_flags=['high_pb'])]))
    path = tmp_path / 'saved.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE research_automation_batches (id TEXT, business_date TEXT, category_id TEXT, strategy_version TEXT, input_hash TEXT, result_json TEXT, created_at TEXT)')
        db.execute('INSERT INTO research_automation_batches VALUES (?,?,?,?,?,?,?)',
            ('b1', source['business_date'], source['category_id'], 'v1', content_hash(source), canonical(source), '2026-10-12 09:10:00'))
        db.execute('CREATE TABLE research_delivery_outbox (id TEXT, status TEXT, attempt_count INTEGER)')
        db.executemany('INSERT INTO research_delivery_outbox VALUES (?,?,?)', [('sent', 'sent', 1), ('unknown', 'delivery_unknown', 1), ('pending', 'pending', 0)])
    return path, source


def test_cli_no_send_no_db_writes_escaped_local_artifacts_missing_name(saved, tmp_path, capsys, monkeypatch):
    import src.config
    monkeypatch.setattr(src.config, 'get_config', lambda: pytest.fail('offline default must not load environment/model config'))
    path, source = saved
    before = path.read_bytes()
    output, html = tmp_path / 'preview.json', tmp_path / 'preview.html'
    assert main(['--db', str(path), '--batch-id', 'b1', '--output-json', str(output), '--output-html', str(html), '--title', '<script>alert(1)</script>']) == 0
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['preview_only'] and result['publish_allowed'] is False
    assert 'id' not in result and 'publication_id' not in result and 'destination_id' not in result
    assert result['business_date'] == '2026-10-12' and result['generated_at'][:10] != '2026-10-12'
    assert '600001' in result['rendered_content'] and 'None' not in result['rendered_content']
    assert '<script>' not in html.read_text(encoding='utf-8')
    assert '&lt;script&gt;' in html.read_text(encoding='utf-8') and '本地预览' in html.read_text(encoding='utf-8')
    assert 'src="http' not in html.read_text(encoding='utf-8')
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM research_delivery_outbox ORDER BY id').fetchall() == [('pending', 'pending', 0), ('sent', 'sent', 1), ('unknown', 'delivery_unknown', 1)]
    assert 'preview_only' in capsys.readouterr().out


@pytest.mark.parametrize('field,value', [('input_hash', 'tampered'), ('category_id', 'etf_rotation'), ('business_date', '2026-10-13'), ('strategy_version', 'other')])
def test_batch_hash_and_bindings_rejected(saved, field, value):
    path, _ = saved
    with sqlite3.connect(path) as db:
        db.execute(f'UPDATE research_automation_batches SET {field}=?', (value,))
    with pytest.raises(ValueError):
        read_batches(path, 'b1')


def test_refuses_overwrite_and_product_source_outputs(saved, tmp_path, capsys):
    path, _ = saved
    assert main(['--db', str(path), '--batch-id', 'b1', '--output-json', str(path)]) == 2
    assert 'output' in capsys.readouterr().out
    assert main(['--db', str(path), '--batch-id', 'b1', '--output-json', 'src/preview.json']) == 2


def test_replay_is_pure_and_uses_all_completed_same_day_batches(saved):
    path, original = saved
    other = deepcopy(original)
    other['category_id'] = 'quality_value'
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO research_automation_batches VALUES (?,?,?,?,?,?,?)',
            ('b2', other['business_date'], other['category_id'], 'v1', content_hash(other), canonical(other), '2026-10-12 09:11:00'))
    source, related, _ = read_batches(path, 'b1')
    before = deepcopy(source)
    result = replay(source, related, top_n=5, title='离线研究')
    assert source == before == original
    assert result['brief_input']['candidates'][0]['code'] == '600001'
    assert result['compared_strategies'] == ['quality_value', 'volume_breakout']
    assert result['source_input_hash'] == content_hash(original)
    assert result['llm']['status'] == 'off'


def test_replay_reports_no_effective_model_adoption_with_candidate_fallback(saved):
    from tests.test_research_delivery_brief import reply
    _, source = saved
    def explain(brief, key):
        return dict(status='succeeded', usage={'completion_tokens': 7}, explanation=reply(brief))
    result = replay(source, [source], explain=explain)
    assert result['llm']['status'] == 'fallback' and result['llm']['reason'] == 'no_effective_model_paragraphs'
    assert result['llm']['usage']['completion_tokens'] == 7 and result['model_paragraphs_adopted'] == 0
    assert '解读：' in result['rendered_content'] and '待观察：' in result['rendered_content']


def test_cli_saved_replay_uses_v4_selection_and_reports_core_adoption(saved):
    from tests.test_research_delivery_signals import selection
    path, source = saved
    before = path.read_bytes()
    calls = []
    def explain(brief, key):
        calls.append(key)
        assert brief['strategy']['expression_version'] == 4
        return dict(status='succeeded', explanation=selection(brief))
    result = replay(source, [source], explain=explain)
    assert len(calls) == 1 and result['llm']['status'] == 'succeeded'
    assert result['model_adoption']['core_complete']
    assert result['model_adoption']['candidate_rationale_count'] == 1
    assert result['preview_only'] and not result['publish_allowed'] and path.read_bytes() == before


def capture(tmp_path, *, source, day='2026-10-12', payload=None, endpoint=None, dataset=None, params=None, columns=None, rows=None):
    body = dict(source=source, business_date=day, fetched_at=day + 'T17:12:00+08:00')
    if endpoint:
        body.update(endpoint=endpoint, params=params, columns=columns, rows=rows)
    else:
        body.update(dataset=dataset, provenance='provider_normalized', payload=payload)
    raw = canonical(body).encode('utf-8')
    path = tmp_path / (sha256(raw).hexdigest() + '.json')
    path.write_bytes(raw)
    entry = {key: value for key, value in body.items() if key not in {'payload', 'columns', 'rows'}}
    entry.update(path=str(path), sha256=sha256(raw).hexdigest())
    if endpoint:
        entry['row_count'] = len(rows)
    return entry


def manifest(tmp_path, name, captures, **extra):
    path = tmp_path / name
    path.write_text(json.dumps(dict(purpose='no_send_acceptance', business_date='2026-10-12', captures=captures, **extra)), encoding='utf-8')
    return path


def test_historical_augmentation_uses_saved_tables_with_original_observation_clocks(saved, tmp_path):
    _, source = saved
    reference = capture(tmp_path, source='tushare', endpoint='stock_basic', params=dict(exchange='', list_status='L'),
        columns=['ts_code', 'name', 'industry'], rows=[['600001.SH', '示例企业', '银行']])
    daily = capture(tmp_path, source='tushare', endpoint='daily', params=dict(trade_date='20261012'),
        columns=['ts_code', 'trade_date', 'pct_chg', 'amount'], rows=[['600001.SH', '20261012', 1., 100.]])
    names = capture(tmp_path, source='instrument_names', dataset='etf_names', payload=dict(instruments=[dict(
        code='510300', name='示例ETF', source='saved-name-provider', asset_type='etf', observed_at='2026-10-12T17:11:00+08:00')]))
    source['evidence'] = [dict(endpoint='daily', params=dict(ts_code='600001.SH', start_date='20260101'), path='not-a-snapshot'), daily]
    source['coverage'] = dict(independent_universe_count=1, daily_response_count=1, daily_response_coverage=1.)
    ref_manifest = manifest(tmp_path, 'reference.json', [reference, names])
    context = saved_context([source], day=source['business_date'], now=datetime.now(ZoneInfo('Asia/Shanghai')),
        evidence_dir=tmp_path, reference_manifest=ref_manifest)
    assert context['business_date'] == '2026-10-12'
    assert context['industries']['as_of'] == '2026-10-12'
    assert context['instruments']['600001']['observed_at'] == reference['fetched_at']
    assert context['instruments']['510300']['name'] == '示例ETF'
    assert len(context['evidence']) == 3
    result = replay(source, [source], context=context)
    assert '示例企业（600001）' in result['rendered_content']
    assert '2026-10-12' in result['rendered_content']
    assert result['generated_at'][:10] != result['business_date']
    assert all(row['fetched_at'].startswith('2026-10-12') for row in result['observations'] if 'fetched_at' in row)
    entry_path = tmp_path / (reference['sha256'] + '.json')
    entry_path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='hash_invalid'):
        saved_context([source], day=source['business_date'], now=datetime.now(ZoneInfo('Asia/Shanghai')), reference_manifest=ref_manifest)


@pytest.mark.parametrize('bad', ['binding', 'entity', 'hash', 'metadata', 'future_disclosure'])
def test_background_manifest_verifies_source_hash_entity_capture_not_mutable_attachment(saved, tmp_path, bad):
    _, source = saved
    day = source['business_date']
    background = dict(issuer_code='600001', business_date=day, missing=[], news=[], announcements=[],
        growth=dict(status='available', source='tushare', fetched_at=day + 'T17:11:00+08:00',
            data=dict(report_date='2026-06-30', announcement_date='2026-08-28', net_profit_yoy=-4.65)))
    if bad == 'entity':
        background['issuer_code'] = '600002'
    if bad == 'future_disclosure':
        background['growth']['data']['announcement_date'] = '2026-11-01'
    entry = capture(tmp_path, source='research_context', dataset='candidate_background',
        payload=dict(version='candidate-background-v1', issuer_code='600001', background=background))
    if bad == 'hash':
        entry['sha256'] = 'invalid'
    if bad == 'metadata':
        entry['fetched_at'] = day + 'T17:00:00+08:00'
    path = manifest(tmp_path, 'background.json', [entry], source_batch_id='b1',
        source_input_hash='invalid' if bad == 'binding' else content_hash(source), background={'600001': {'unverified': True}})
    with pytest.raises(ValueError):
        saved_context([source], day=day, now=datetime.now(ZoneInfo('Asia/Shanghai')), background_manifest=path,
            bindings=[dict(id='b1', input_hash=content_hash(source))])


def test_verified_background_is_reused_without_fetch_and_mutable_manifest_background_is_ignored(saved, tmp_path):
    _, source = saved
    day = source['business_date']
    background = dict(issuer_code='600001', business_date=day, status='partial', missing=[], news=[], announcements=[],
        growth=dict(status='available', source='tushare', fetched_at=day + 'T17:11:00+08:00',
            data=dict(report_date='2026-06-30', announcement_date='2026-08-28', net_profit_yoy=-4.65)))
    entry = capture(tmp_path, source='research_context', dataset='candidate_background',
        payload=dict(version='candidate-background-v1', issuer_code='600001', background=background))
    path = manifest(tmp_path, 'background.json', [entry], source_batch_id='b1', source_input_hash=content_hash(source),
        background={'600001': {'growth': 'mutated untrusted value'}})
    context = saved_context([source], day=day, now=datetime.now(ZoneInfo('Asia/Shanghai')), background_manifest=path,
        bindings=[dict(id='b1', input_hash=content_hash(source))])
    assert context['background']['600001']['growth']['data']['net_profit_yoy'] == -4.65
    assert context['background']['600001']['news'] == []  # zero qualifying rows is not source failure


@pytest.mark.parametrize('domain', ['news', 'announcements'])
@pytest.mark.parametrize('observed', ['2026-10-13T17:00:00+08:00', '2026-10-12T19:00:00+08:00', '2026-10-12T17:11:00+08:00'])
def test_background_lead_observation_clock_cannot_be_relabelled_as_historical(saved, tmp_path, domain, observed):
    _, source = saved
    day = source['business_date']
    lead = dict(issuer_code='600001', title='600001示例企业公告', published_date='2026-10-11', fetched_at=observed,
        evidence_kind='company_news_lead' if domain == 'news' else 'announcement_metadata',
        source='search' if domain == 'news' else 'EastmoneyAnnouncement',
        url='https://example.com/news' if domain == 'news' else 'https://data.eastmoney.com/notices/detail/600001/AN202610110001.html')
    background = dict(issuer_code='600001', business_date=day, status='partial', missing=[], news=[], announcements=[])
    background[domain] = [lead]
    entry = capture(tmp_path, source='research_context', dataset='candidate_background',
        payload=dict(version='candidate-background-v1', issuer_code='600001', background=background))
    path = manifest(tmp_path, 'background.json', [entry], source_batch_id='b1', source_input_hash=content_hash(source))
    kwargs = dict(day=day, now=datetime.fromisoformat('2026-10-12T18:00:00+08:00'), background_manifest=path,
        bindings=[dict(id='b1', input_hash=content_hash(source))])
    if observed != '2026-10-12T17:11:00+08:00':
        with pytest.raises(ValueError):
            saved_context([source], **kwargs)
    else:
        context = saved_context([source], **kwargs)
        assert context['background']['600001'][domain] == [lead]


@pytest.mark.parametrize('domain', ['news', 'announcements'])
@pytest.mark.parametrize('field,value', [('issuer_code', '600002'), ('published_date', '2026-10-13'), ('evidence_kind', 'unverified'), ('url', 'http://example.com/untrusted')])
def test_background_lead_original_entity_publication_and_provider_contract_are_verified(saved, tmp_path, domain, field, value):
    _, source = saved
    day = source['business_date']
    lead = dict(issuer_code='600001', title='600001示例企业公告', published_date='2026-10-11', fetched_at=day + 'T17:11:00+08:00',
        evidence_kind='company_news_lead' if domain == 'news' else 'announcement_metadata',
        source='search' if domain == 'news' else 'EastmoneyAnnouncement',
        url='https://example.com/news' if domain == 'news' else 'https://data.eastmoney.com/notices/detail/600001/AN202610110001.html')
    lead[field] = value
    background = dict(issuer_code='600001', business_date=day, status='partial', missing=[], news=[], announcements=[])
    background[domain] = [lead]
    entry = capture(tmp_path, source='research_context', dataset='candidate_background',
        payload=dict(version='candidate-background-v1', issuer_code='600001', background=background))
    path = manifest(tmp_path, 'background.json', [entry], source_batch_id='b1', source_input_hash=content_hash(source))
    with pytest.raises(ValueError):
        saved_context([source], day=day, now=datetime.fromisoformat('2026-10-12T18:00:00+08:00'), background_manifest=path,
            bindings=[dict(id='b1', input_hash=content_hash(source))])


@pytest.mark.parametrize('changed', [False, True])
def test_snapshot_valuation_without_provider_timestamp_must_match_bound_original_candidate(saved, tmp_path, changed):
    _, source = saved
    day = source['business_date']
    source['result']['picks'][0].update(pe_ratio=12.5, pb_ratio=1.2)
    valuation = dict(status='available', source='screening_snapshot', evidence_ids=[],
        data=dict(as_of=day, pe_ratio=12.5, pb_ratio=9.9 if changed else 1.2))
    background = dict(issuer_code='600001', business_date=day, status='partial', missing=[], news=[], announcements=[], valuation=valuation)
    entry = capture(tmp_path, source='research_context', dataset='candidate_background',
        payload=dict(version='candidate-background-v1', issuer_code='600001', background=background))
    path = manifest(tmp_path, 'background.json', [entry], source_batch_id='b1', source_input_hash=content_hash(source))
    kwargs = dict(day=day, now=datetime.now(ZoneInfo('Asia/Shanghai')), background_manifest=path,
        bindings=[dict(id='b1', input_hash=content_hash(source))])
    if changed:
        with pytest.raises(ValueError):
            saved_context([source], **kwargs)
    else:
        context = saved_context([source], **kwargs)
        assert context['background']['600001']['valuation'] == valuation
        assert 'fetched_at' not in context['background']['600001']['valuation']


def test_opt_in_ledger_uses_existing_budget_cache_and_denies_nonledger_access(saved):
    path, _ = saved
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError
    from src.repositories.research_delivery_models import ResearchLLMAttempt
    from sqlalchemy import create_engine
    engine = create_engine('sqlite:///' + path.as_posix())
    ResearchLLMAttempt.__table__.create(engine)
    engine.dispose()
    ledger = LedgerOnly(path)
    try:
        first = ledger.reserve_llm('key', '2026-10-16')
        assert first['created']
        ledger.finish_llm(first['id'], dict(status='succeeded', explanation=None))
        assert ledger.reserve_llm('key', '2026-10-16')['result']['status'] == 'succeeded'
        for i in range(19):
            assert ledger.reserve_llm(f'key{i}', '2026-10-16')['created']
        assert ledger.reserve_llm('over-budget', '2026-10-16') is None
        for sql in ['UPDATE research_delivery_outbox SET attempt_count=9', 'SELECT * FROM research_delivery_outbox', 'CREATE TABLE unsafe (id TEXT)']:
            with pytest.raises(DatabaseError), ledger.engine.connect() as connection:
                connection.execute(text(sql))
    finally:
        ledger.engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM research_delivery_llm_attempts').fetchone()[0] == 20
        assert db.execute('SELECT id,status,attempt_count FROM research_delivery_outbox ORDER BY id').fetchall() == [('pending', 'pending', 0), ('sent', 'sent', 1), ('unknown', 'delivery_unknown', 1)]


def test_model_flag_uses_existing_explanation_once_key_with_only_ledger_writes(saved, tmp_path, monkeypatch):
    import src.config
    import src.services.research_delivery.explanation as explanation
    from sqlalchemy import create_engine
    from src.repositories.research_delivery_models import ResearchLLMAttempt
    path, _ = saved
    engine = create_engine('sqlite:///' + path.as_posix())
    ResearchLLMAttempt.__table__.create(engine)
    engine.dispose()
    config = src.config.Config(litellm_model='preview-route', llm_models_source='yaml', llm_model_list=[
        dict(model_name='preview-route', litellm_params=dict(model='openai/synthetic', api_key='fake-key', api_base='https://example.invalid/v1'))])
    monkeypatch.setattr(src.config, 'get_config', lambda: config)
    calls = []
    def complete(prompt, *args, **kwargs):
        brief = json.loads(prompt.removeprefix('<frozen_input>').removesuffix('</frozen_input>'))
        calls.append(brief)
        return json.dumps(dict(summary=[], cross_signals=[], candidates=[dict(code=row['code'], rationale=[], watch=[], invalidation=[]) for row in brief['candidates']])), 'openai/synthetic', {}
    monkeypatch.setattr(explanation, 'make_completion', lambda route: complete)
    for index in range(2):
        output = tmp_path / f'model-{index}.json'
        assert main(['--db', str(path), '--batch-id', 'b1', '--use-model', '--output-json', str(output)]) == 0
        assert json.loads(output.read_text(encoding='utf-8'))['llm']['status'] == 'fallback'
        assert json.loads(output.read_text(encoding='utf-8'))['llm']['reason'] == 'no_effective_model_paragraphs'
    assert len(calls) == 1
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM research_delivery_llm_attempts').fetchone()[0] == 1
        assert db.execute('SELECT id,status,attempt_count FROM research_delivery_outbox ORDER BY id').fetchall() == [('pending', 'pending', 0), ('sent', 'sent', 1), ('unknown', 'delivery_unknown', 1)]
