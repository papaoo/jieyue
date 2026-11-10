"""Offline shortlist enrichment: immutable inputs, dated facts and shared requests."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.services.research_delivery.live_contracts import ProducerResult
from src.services.research_delivery.research_context import ResearchContextEnricher


DAY = '2026-10-15'
NOW = datetime(2026, 10, 15, 18, tzinfo=ZoneInfo('Asia/Shanghai'))


def source(category='one', codes=('600001', '600002', '600003')):
    return ProducerResult(category, DAY, 'ready', result={'picks': [dict(code=code, name='公司' + code,
        rank=i + 1, final_score=80 - i, pe_ratio=12, pb_ratio=1.5) for i, code in enumerate(codes)]})


def blocks(code, **kwargs):
    return {'growth': dict(status='ok', source='tushare', fetched_at=NOW.isoformat(),
        data=dict(issuer_code=code, report_date='2026-06-30', announcement_date='2026-08-20', roe=9.1))}


def enricher(tmp_path, fundamental=None, news=None, announcement=None, **kw):
    return ResearchContextEnricher(SimpleNamespace(), fundamental_fetcher=fundamental or Mock(side_effect=blocks),
        news_fetcher=news or Mock(return_value=SimpleNamespace(success=True, results=[], provider='test')),
        announcement_fetcher=announcement or Mock(return_value=dict(ok=True, records=[], fetched_at=NOW.isoformat())),
        clock=lambda: NOW, evidence_dir=tmp_path, **kw)


def test_topn_shared_no_mutation_and_order_independent_cross_signals(tmp_path):
    fetch = Mock(side_effect=blocks)
    service = enricher(tmp_path, fetch)
    first, other = source(), source('two', ('600002', '600001'))
    before = deepcopy(first.to_dict())
    result = service.enrich(first, related_results=[first, other], top_n_by_strategy={'one': 2, 'two': 1})
    service.enrich(other, related_results=[first, other], top_n_by_strategy={'one': 2, 'two': 1})
    service.enrich(first, related_results=[first, other], top_n_by_strategy={'one': 2, 'two': 1})
    assert first.to_dict() == before and result.result['picks'] == before['result']['picks']
    assert fetch.call_count == 2
    context = result.result['research_context']
    assert context['business_date'] == DAY and set(context['background']) == {'600001', '600002'}
    assert context['compared_strategies'] == ['one', 'two']
    assert context['cross_strategy']['600002'] == ['one', 'two']
    assert context['background']['600001']['valuation']['data']['pe_ratio'] == 12
    assert result.evidence and result.evidence[0]['dataset'] == 'candidate_background'


def test_only_completed_same_day_results_count(tmp_path):
    current = source()
    related = [replace(current, category_id='old', business_date='2026-10-14'),
        replace(current, category_id='blocked', state='blocked'),
        replace(current, category_id='bad', blockers=['bad'])]
    result = enricher(tmp_path).enrich(current, related_results=related,
        top_n_by_strategy={'one': 1, 'old': 1, 'blocked': 1, 'bad': 1})
    assert result.result['research_context']['compared_strategies'] == ['one']


@pytest.mark.parametrize('change', [dict(issuer_code='600999'), dict(announcement_date='2026-10-16'),
    dict(report_date='2026-12-31'), dict(announcement_date=None)])
def test_financial_issuer_and_disclosure_date_required(tmp_path, change):
    def fetch(code, **kw):
        data = blocks(code)
        data['growth']['data'].update(change)
        return data
    result = enricher(tmp_path, fetch).enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    assert not result.result['research_context']['background']['600001']['growth']['data']


def test_old_quote_rejected_but_disclosed_prior_financial_report_accepted(tmp_path):
    def fetch(code, **kw):
        return dict(blocks(code), valuation=dict(status='ok', source='tushare', fetched_at=NOW.isoformat(),
            data=dict(issuer_code=code, as_of='2026-10-14', pe_ratio=99)))
    result = enricher(tmp_path, fetch).enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    background = result.result['research_context']['background']['600001']
    assert background['growth']['data']['roe'] == 9.1
    assert background['valuation']['data']['pe_ratio'] == 12
    assert 'valuation_stale_or_invalid' in background['missing']


def test_news_is_only_dated_company_lead_and_announcements_exact_issuer(tmp_path):
    news = Mock(return_value=SimpleNamespace(success=True, provider='test', results=[
        SimpleNamespace(title='公司600001 签约', content='', url='https://example.com/a', published_date=DAY),
        SimpleNamespace(title='公司600999 签约', content='', url='https://example.com/b', published_date=DAY),
        SimpleNamespace(title='600001 旧闻', content='', url='https://example.com/c', published_date='2026-09-01')]))
    ann = dict(issuer_code='600001', title='公告', url='https://data.eastmoney.com/notices/detail/600001/AN1.html',
        source='EastmoneyAnnouncement', published_date=DAY, evidence_kind='announcement_metadata')
    announcements = Mock(return_value=dict(ok=True, fetched_at=NOW.isoformat(), records=[ann,
        dict(ann, issuer_code='600999'), dict(ann, published_date='2026-10-16')]))
    result = enricher(tmp_path, news=news, announcement=announcements).enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    background = result.result['research_context']['background']['600001']
    assert len(background['news']) == len(background['announcements']) == 1
    assert background['news'][0]['evidence_kind'] == 'company_news_lead'
    assert background['announcements'][0]['evidence_kind'] == 'announcement_metadata'


def test_provider_failure_is_safe_and_guard_exceptions_propagate(tmp_path):
    result = enricher(tmp_path, Mock(side_effect=RuntimeError('PRIVATE_SENTINEL'))).enrich(
        source(), related_results=[], top_n_by_strategy={'one': 1})
    assert result.state == 'ready' and result.result['picks'] == source().result['picks']
    assert 'PRIVATE_SENTINEL' not in str(result.to_dict())
    error = RuntimeError('guard')
    with pytest.raises(RuntimeError, match='guard'):
        enricher(tmp_path).enrich(source(), related_results=[], top_n_by_strategy={'one': 1},
            progress=Mock(side_effect=error))


def test_partial_blocks_survive_budget_and_restart_rehydrates_verified_evidence(tmp_path):
    monotonic = [0.]
    def fetch(code, on_block, **kw):
        on_block('growth', blocks(code)['growth'])
        monotonic[0] += 91
        raise TimeoutError()
    first = enricher(tmp_path, fetch, monotonic=lambda: monotonic[0]).enrich(source(), related_results=[], top_n_by_strategy={'one': 2})
    background = first.result['research_context']['background']
    assert background['600001']['growth']['data']['roe'] == 9.1
    assert background['600002']['status'] == 'budget_exhausted'
    fetch_again = Mock(side_effect=blocks)
    next_result = enricher(tmp_path, fetch_again).enrich(source('two'), related_results=[first], top_n_by_strategy={'one': 2, 'two': 2})
    assert fetch_again.call_count == 0
    assert next_result.result['research_context']['background']['600001']['growth']['data']['roe'] == 9.1


def test_daily_union_cap_and_next_day_reset(tmp_path):
    service = enricher(tmp_path)
    many = [source(str(i), tuple(str(600000 + i * 20 + j) for j in range(20))) for i in range(6)]
    limits = {str(i): 20 for i in range(6)}
    for item in many:
        service.enrich(item, related_results=many, top_n_by_strategy=limits)
    assert len(service._requested[DAY]) == 100
    last = service.enrich(many[-1], related_results=many, top_n_by_strategy=limits)
    assert all(row['status'] == 'daily_limit' for row in last.result['research_context']['background'].values())
    assert last.result['research_context']['cross_strategy']['600119'] == ['5']


def test_actual_adapter_shares_market_tail_and_filters_wrong_issuer_and_future_rows(tmp_path, monkeypatch):
    import data_provider.tushare_fetcher as module
    calls = []
    def query(api, **kw):
        calls.append((api, kw))
        code = kw.get('ts_code')
        if not code:
            return pd.DataFrame()  # Even failed whole-market requests are shared.
        if api == 'stock_basic':
            return pd.DataFrame([dict(ts_code=code, name='公司', industry='银行')])
        if api == 'daily_basic':
            return pd.DataFrame([dict(ts_code=code, trade_date='20261015', pe_ttm=8, pb=1, total_mv=9, circ_mv=8)])
        if api == 'fina_indicator':
            return pd.DataFrame([dict(ts_code='600999.SH', end_date='20260630', ann_date='20260820', roe=100),
                dict(ts_code=code, end_date='20260630', ann_date='20261016', roe=99),
                dict(ts_code=code, end_date='20260630', ann_date='20260820', roe=9)])
        return pd.DataFrame()
    monkeypatch.setattr(module, 'create_tushare_http_client', lambda *a, **kw: SimpleNamespace(query=query))
    service = ResearchContextEnricher(SimpleNamespace(tushare_token='test'), clock=lambda: NOW, evidence_dir=tmp_path,
        news_fetcher=Mock(return_value=SimpleNamespace(success=True, results=[], provider='test')),
        announcement_fetcher=Mock(return_value=dict(ok=True, records=[], fetched_at=NOW.isoformat())))
    result = service.enrich(source(), related_results=[], top_n_by_strategy={'one': 2})
    assert result.result['research_context']['background']['600001']['growth']['data']['roe'] == 9
    market = [(api, str({k: v for k, v in kw.items() if k != 'request_timeout'})) for api, kw in calls if not kw.get('ts_code')]
    assert len(market) == len(set(market))


def test_rehydrate_ignores_tampered_context_and_rejects_tampered_capture(tmp_path):
    original = enricher(tmp_path).enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    altered = deepcopy(original.to_dict())
    altered['result']['research_context']['background']['600001']['growth']['data']['roe'] = 999
    fetch = Mock(side_effect=blocks)
    service = enricher(tmp_path, fetch)
    restored = service.enrich(source('two'), related_results=[altered], top_n_by_strategy={'one': 1, 'two': 1})
    assert fetch.call_count == 0
    assert restored.result['research_context']['background']['600001']['growth']['data']['roe'] == 9.1
    from pathlib import Path
    capture = Path(original.evidence[0]['path'])
    # Deliberate test corruption, not a real report/database mutation.
    capture.write_text('{}', encoding='utf8')
    fetch = Mock(side_effect=blocks)
    # Same bytes would have the same content-addressed path: do not overwrite
    # corrupted evidence. Automation will keep the base publication usable.
    with pytest.raises(ValueError, match='evidence_hash_conflict'):
        enricher(tmp_path, fetch).enrich(source('two'), related_results=[altered], top_n_by_strategy={'one': 1, 'two': 1})
    assert fetch.call_count == 1


def test_rehydrate_attempts_from_now_disabled_strategy_still_count_towards_daily_cap(tmp_path):
    first = enricher(tmp_path).enrich(source('disabled'), related_results=[], top_n_by_strategy={'disabled': 2})
    fetch = Mock(side_effect=blocks)
    current = enricher(tmp_path, fetch).enrich(source('two'), related_results=[first], top_n_by_strategy={'two': 2})
    assert fetch.call_count == 0
    assert current.result['research_context']['background_scope']['requested_count'] == 2
    assert current.result['research_context']['compared_strategies'] == ['two']


def test_budget_forwarding_and_next_business_date_cache_reset(tmp_path):
    now, elapsed = [NOW], [0.]
    fetch = Mock(side_effect=blocks)
    news = Mock(side_effect=lambda *a, **kw: (elapsed.__setitem__(0, 89.) or SimpleNamespace(success=True, results=[])))
    service = ResearchContextEnricher(SimpleNamespace(), fundamental_fetcher=fetch, news_fetcher=news,
        announcement_fetcher=Mock(side_effect=AssertionError('residual exhausted')), clock=lambda: now[0],
        evidence_dir=tmp_path, monotonic=lambda: elapsed[0])
    result = service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    assert fetch.call_args.kwargs['budget_seconds'] == 8
    assert news.call_args.kwargs['timeout_seconds'] <= 87
    assert result.result['research_context']['background']['600001']['statuses']['announcements'] == 'budget_exhausted'
    now[0] += timedelta(days=1)
    service.enrich(replace(source(), business_date=now[0].date().isoformat()), related_results=[result], top_n_by_strategy={'one': 1})
    assert fetch.call_count == 2 and set(service._requested) == {'2026-10-16'}


def test_progress_interruption_inside_actual_adapter_is_not_provider_failure(tmp_path, monkeypatch):
    import data_provider.tushare_fetcher as module
    from src.repositories.research_delivery_repository import ManagementError
    calls = []
    monkeypatch.setattr(module, 'create_tushare_http_client', lambda *a, **kw: SimpleNamespace(query=lambda *a, **kw: calls.append(a) or pd.DataFrame()))
    count = [0]
    def progress(*a, **kw):
        count[0] += 1
        if count[0] == 3:  # Third callback is inside adapter's query fallback.
            raise ManagementError('lease_lost', 'guard', 409)
    service = ResearchContextEnricher(SimpleNamespace(tushare_token='test'), clock=lambda: NOW, evidence_dir=tmp_path)
    with pytest.raises(ManagementError, match='guard'):
        service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1}, progress=progress)
    assert not calls and not list(tmp_path.rglob('*.json'))


def _slow_announcement_worker(connection, code, target):
    import time
    time.sleep(10)


def test_actual_announcement_deadline_reaps_process_without_network(monkeypatch):
    import multiprocessing
    import time
    import src.services.research_delivery.research_context as module
    monkeypatch.setattr(module, '_announcement_worker', _slow_announcement_worker)
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        module._bounded_announcements('600001', as_of=NOW.date(), timeout_seconds=.05)
    assert time.monotonic() - started < 4
    assert {child.pid for child in multiprocessing.active_children()} <= before


def test_adapter_domain_gaps_and_dividend_holder_scope_preserved(tmp_path):
    def fetch(code, **kw):
        base = dict(source='tushare', fetched_at=NOW.isoformat(), status='partial', missing_fields=['full_institution_coverage'])
        return dict(institution=dict(base, data=dict(issuer_code=code, report_date='2026-06-30',
            announcement_date='2026-08-20', coverage='top10_float_holders_only', full_institution_coverage=False)),
            earnings=dict(base, missing_fields=['operating_cash_flow'], data=dict(
                financial_report=dict(issuer_code=code, report_date='2026-06-30', announcement_date='2026-08-20', revenue=100),
                dividend=dict(issuer_code=code, as_of_events=DAY, ttm_cash_dividend_per_share=.8,
                    events_complete=True, coverage='disclosed_events'))))
    background = enricher(tmp_path, fetch).enrich(source(), related_results=[], top_n_by_strategy={'one': 1}).result['research_context']['background']['600001']
    assert background['institution']['data']['full_institution_coverage'] is False
    assert background['institution']['status'] == 'partial'
    assert background['dividend']['data']['ttm_cash_dividend_per_share'] == .8
    assert 'financial_report:operating_cash_flow' in background['missing']


def test_same_category_frozen_background_reused_after_restart(tmp_path):
    first = enricher(tmp_path).enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    fetch = Mock(side_effect=blocks)
    second = enricher(tmp_path, fetch).enrich(source(), related_results=[first], top_n_by_strategy={'one': 1})
    assert fetch.call_count == 0
    assert second.result['research_context']['background']['600001']['growth']['data']['roe'] == 9.1


def test_evidence_capture_failure_never_repeats_provider_or_uses_unsupported_facts(tmp_path):
    fetch = Mock(side_effect=blocks)
    service = enricher(tmp_path, fetch)
    service.store.capture_payload = Mock(side_effect=OSError('PRIVATE_SENTINEL'))
    with pytest.raises(OSError):
        service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    result = service.enrich(source('two'), related_results=[], top_n_by_strategy={'two': 1})
    assert fetch.call_count == 1
    assert result.result['research_context']['background']['600001']['status'] == 'evidence_capture_failed'
    assert 'growth' not in result.result['research_context']['background']['600001']


def test_pure_default_constructor_returns_normalized_inline_evidence():
    service = ResearchContextEnricher(SimpleNamespace(), fundamental_fetcher=blocks, clock=lambda: NOW,
        news_fetcher=lambda *a, **kw: SimpleNamespace(success=True, results=[]),
        announcement_fetcher=lambda *a, **kw: dict(ok=True, records=[], fetched_at=NOW.isoformat()))
    result = service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    assert result.evidence[0]['payload']['issuer_code'] == '600001'
    assert result.evidence[0]['sha256'] == result.result['research_context']['background']['600001']['evidence_id']


@pytest.mark.parametrize('code', ['lease_lost', 'authorization_changed'])
def test_cancel_after_success_keeps_partial_attempt_and_resume_never_refetches(tmp_path, code):
    from src.repositories.research_delivery_repository import ManagementError
    completed = [False]
    def fetch(symbol, **kw):
        completed[0] = True
        return blocks(symbol)
    fundamental = Mock(side_effect=fetch)
    news = Mock(return_value=SimpleNamespace(success=True, results=[]))
    service = enricher(tmp_path, fundamental, news=news)
    error = ManagementError(code, 'guard', 409)
    def cancel(*a, **kw):
        if completed[0]:
            raise error
    with pytest.raises(ManagementError) as caught:
        service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1}, progress=cancel)
    assert caught.value is error
    assert fundamental.call_count == 1 and news.call_count == 0
    assert not list(tmp_path.rglob('*.json'))  # Cancellation cannot authorize a capture.
    result = service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    assert fundamental.call_count == 1 and news.call_count == 0
    background = result.result['research_context']['background']['600001']
    assert background['growth']['data']['roe'] == 9.1
    assert background['status'] == 'interrupted'
    assert result.evidence and background['evidence_id'] == result.evidence[0]['sha256']
    restarted = enricher(tmp_path, fundamental)
    resumed = restarted.enrich(source('two'), related_results=[result], top_n_by_strategy={'two': 1})
    assert fundamental.call_count == 1
    assert resumed.result['research_context']['background_scope']['requested_count'] == 1


@pytest.mark.parametrize('provider_domain,domain,old_gap,current_gaps', [
    ('earnings', 'dividend', 'dividend_events', []),
    ('dragon_tiger', 'dragon_tiger', 'multi_session_history', []),
    ('boards', 'boards', 'sector_rankings', ['industry_constituent_coverage']),
    ('capital_flow', 'stock_flow', 'sector_rankings', ['industry_constituent_coverage']),
])
def test_domain_replacement_retires_only_superseded_gaps(tmp_path, provider_domain, domain, old_gap, current_gaps):
    def fetch(code, on_block, **kw):
        unrelated = blocks(code)['growth']
        on_block('growth', dict(unrelated, status='partial', missing_fields=['gross_margin']))
        facts = {
            'earnings': dict(dividend=dict(issuer_code=code, as_of_events=DAY,
                events_complete=True, ttm_cash_dividend_per_share=.8),
                financial_report=dict(issuer_code=code, report_date='2026-06-30', announcement_date='2026-08-20', revenue=100)),
            'dragon_tiger': dict(issuer_code=code, checked_trade_date=DAY, is_on_list=False, lookback_sessions=5),
            'boards': dict(issuer_code=code, industry='银行'),
            'capital_flow': dict(stock_flow=dict(issuer_code=code, as_of=DAY, main_net_inflow=100)),
        }
        base = dict(source='tushare', fetched_at=NOW.isoformat(), data=facts[provider_domain])
        on_block(provider_domain, dict(base, status='partial', missing_fields=[old_gap]))
        return {provider_domain: dict(base, status='partial' if current_gaps else 'ok', missing_fields=current_gaps)}
    original = source()
    original.result['picks'][0]['risk_flags'] = ['原始估值风险']
    before = deepcopy(original.to_dict())
    result = enricher(tmp_path, fetch).enrich(original, related_results=[], top_n_by_strategy={'one': 1})
    background = result.result['research_context']['background']['600001']
    assert domain + ':' + old_gap not in background['missing']
    assert background[domain]['missing_fields'] == current_gaps
    assert set(background['missing']) == {'growth:gross_margin', *(domain + ':' + gap for gap in current_gaps)}
    assert original.to_dict() == before and result.result['picks'] == before['result']['picks']


def test_domain_replacement_clears_stale_marker_and_preserves_unrelated_gaps(tmp_path):
    def fetch(code, on_block, **kw):
        growth = blocks(code)['growth']
        on_block('boards', dict(growth, status='partial', missing_fields=['industry_constituent_coverage'],
            data=dict(issuer_code=code, industry='银行')))
        on_block('growth', dict(growth, status='partial', missing_fields=['roe'],
            data=dict(growth['data'], issuer_code='600999')))
        return {'growth': dict(growth, missing_fields=[])}
    result = enricher(tmp_path, fetch).enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    background = result.result['research_context']['background']['600001']
    assert background['growth']['status'] == 'available'
    assert background['missing'] == ['boards:industry_constituent_coverage']


def test_domain_replacement_keeps_latest_gaps_when_cancelled(tmp_path):
    from src.repositories.research_delivery_repository import ManagementError
    latest = [False]
    error = ManagementError('lease_lost', 'guard', 409)
    def fetch(code, on_block, **kw):
        growth = dict(blocks(code)['growth'], status='partial')
        on_block('growth', dict(growth, missing_fields=['gross_margin']))
        latest[0] = True
        on_block('growth', dict(growth, missing_fields=['revenue_yoy']))
        pytest.fail('latest-state progress must interrupt fetch')
    fetcher = Mock(side_effect=fetch)
    service = enricher(tmp_path, fetcher)
    def cancel(*a, **kw):
        if latest[0]:
            raise error
    with pytest.raises(ManagementError) as caught:
        service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1}, progress=cancel)
    assert caught.value is error and not list(tmp_path.rglob('*.json'))
    result = service.enrich(source(), related_results=[], top_n_by_strategy={'one': 1})
    background = result.result['research_context']['background']['600001']
    assert fetcher.call_count == 1
    assert background['growth']['missing_fields'] == ['revenue_yoy']
    assert set(background['missing']) == {'growth:revenue_yoy', 'enrichment_interrupted'}
