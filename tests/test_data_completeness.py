"""Regressions for factual coverage, price basis and missing-data disclosure."""
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest


def test_tushare_adjustment_rejects_missing_factor_and_marks_basis(monkeypatch):
    from src.services.screening.daily import _apply_tushare_adjustment
    raw = pd.DataFrame({'trade_date': ['20261008', '20261009'],
                        'open': [10, 5], 'high': [10, 5], 'low': [10, 5], 'close': [10, 5]})
    api = SimpleNamespace(adj_factor=lambda **kw: pd.DataFrame({'trade_date': ['20261009'], 'adj_factor': [2]}))
    with pytest.raises(RuntimeError, match='factor'):
        _apply_tushare_adjustment(raw, pro=api, ts_code='600000.SH', start_date='20261008', end_date='20261009', adj='qfq')
    api.adj_factor = lambda **kw: pd.DataFrame({'trade_date': ['20261008', '20261009'], 'adj_factor': [1, 2]})
    adjusted = _apply_tushare_adjustment(raw, pro=api, ts_code='600000.SH', start_date='20261008', end_date='20261009', adj='qfq')
    assert adjusted.close.tolist() == [5, 5]
    assert adjusted.attrs['price_adjustment'] == 'forward'


def test_daily_bridge_does_not_accept_unadjusted_as_forward(monkeypatch):
    from src.services import screening_service as service
    from src.services.screening import daily
    raw = pd.DataFrame({'date': ['2026-10-09'], 'close': [10]})
    adjusted = raw.copy()
    adjusted.attrs.update(price_adjustment='forward', daily_source='tushare')
    monkeypatch.setattr(service, 'get_dsa_daily_history', lambda *a, **kw: (raw, 'TushareFetcher'))
    monkeypatch.setattr(daily, 'daily_history_is_stale', lambda *a, **kw: False)
    fallback = Mock(return_value=adjusted)
    monkeypatch.setattr(daily, 'fetch_daily_history', fallback)
    monkeypatch.setenv('TUSHARE_DAILY_ADJ', 'qfq')
    result = service._build_screening_dsa_daily_history_fetcher()('600000', source='auto')
    assert fallback.call_count == 1
    assert result.attrs['price_adjustment'] == 'forward'


def _tables():
    return {
        'stock_basic': [{'ts_code': '601166.SH', 'name': '兴业银行', 'industry': '银行'}],
        'daily_basic': [{'ts_code': '601166.SH', 'trade_date': '20261009', 'pe_ttm': 5, 'pb': 0.5, 'total_mv': 10, 'circ_mv': 8, 'close': 18, 'dv_ttm': 4}],
        'fina_indicator': [{'ts_code': '601166.SH', 'ann_date': '20260825', 'end_date': '20260630', 'roe': 0, 'tr_yoy': 2, 'netprofit_yoy': 3, 'grossprofit_margin': None}],
        'income': [{'ts_code': '601166.SH', 'ann_date': '20260825', 'end_date': '20260630', 'total_revenue': 100, 'n_income_attr_p': 10}],
        'cashflow': [{'ts_code': '601166.SH', 'ann_date': '20260825', 'end_date': '20260630', 'n_cashflow_act': 20}],
        'top10_floatholders': [{'ts_code': '601166.SH', 'ann_date': '20260825', 'end_date': '20260630', 'holder_name': 'holder', 'hold_amount': 3}],
        'moneyflow': [{'ts_code': '601166.SH', 'trade_date': '20261009', 'buy_lg_amount': 10, 'buy_elg_amount': 20, 'sell_lg_amount': 4, 'sell_elg_amount': 6, 'net_mf_amount': 8}],
        'top_list': [],
    }


def test_native_fundamentals_preserve_zero_units_dates_and_partial_failures():
    from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter
    tables = _tables()
    calls = []
    def query(api, **params):
        calls.append((api, params))
        if api == 'cashflow':
            raise TimeoutError('test timeout')
        return pd.DataFrame(tables[api])
    result = TushareFundamentalAdapter(query).fetch('601166', budget_seconds=10, as_of='2026-10-12')
    assert result['valuation']['data']['total_mv'] == 100_000
    assert result['growth']['data']['roe'] == 0
    assert result['growth']['data']['report_date'] == '2026-06-30'
    assert result['earnings']['status'] == 'partial'
    assert result['earnings']['data']['financial_report']['operating_cash_flow'] is None
    assert result['earnings']['data']['dividend']['ttm_dividend_yield_pct'] == 4
    assert result['earnings']['data']['dividend']['as_of'] == '2026-10-09'
    assert result['capital_flow']['data']['stock_flow']['main_net_inflow'] == 200_000
    assert result['capital_flow']['data']['stock_flow']['provider_net_inflow'] == 80_000
    assert result['boards']['data']['industry'] == '银行'
    assert result['dragon_tiger']['data']['is_on_list'] is False
    assert next(params for api, params in calls if api == 'top_list')['trade_date'] == '20261009'


def test_native_fundamentals_reject_future_and_wrong_company():
    from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter
    tables = _tables()
    tables['fina_indicator'][0]['ann_date'] = '20261020'
    tables['income'][0]['ts_code'] = '600000.SH'
    result = TushareFundamentalAdapter(lambda api, **kw: pd.DataFrame(tables[api])).fetch('601166', budget_seconds=10, as_of='2026-10-12')
    assert result['growth']['status'] != 'ok'
    assert result['earnings']['data'].get('financial_report', {}).get('revenue') is None


def test_native_manager_caches_facts_and_keeps_fallback_evidence(monkeypatch):
    from data_provider.base import DataFetcherManager
    from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter
    cfg = SimpleNamespace(tushare_token='test-only', enable_fundamental_pipeline=True, fundamental_cache_ttl_seconds=60,
                          fundamental_stage_timeout_seconds=5, fundamental_fetch_timeout_seconds=1,
                          fundamental_retry_max=0)
    native = TushareFundamentalAdapter(lambda api, **kw: pd.DataFrame(_tables()[api])).fetch(
        '601166', budget_seconds=10, as_of='2026-10-12')
    getter = Mock(side_effect=lambda *a, on_block=None, **kw: native)
    provider = SimpleNamespace(name='TushareFetcher', priority=0, get_fundamental_snapshot=getter)
    manager = DataFetcherManager(fetchers=[provider])
    fallback = Mock(return_value={'capital_flow': {'status': 'partial', 'data': {
        'sector_rankings': {'top': [{'name': '银行'}]}}, 'errors': [], 'source_chain': []}})
    monkeypatch.setattr(manager, '_get_legacy_fundamental_context', fallback)
    monkeypatch.setattr('src.config.get_config', lambda: cfg)
    first = manager.get_fundamental_context('601166')
    assert first['capital_flow']['data']['stock_flow']['main_net_inflow'] == 200000
    assert first['capital_flow']['data']['sector_rankings']['top'][0]['name'] == '银行'
    first['growth']['data']['roe'] = 999
    assert manager.get_fundamental_context('601166')['growth']['data']['roe'] == 0
    assert getter.call_count == 1


def test_news_drops_all_zero_relevance_results():
    from src.search_service import SearchResult, SearchResponse, SearchService
    response = SearchResponse(query='民生银行', provider='test', success=True, results=[
        SearchResult(title='HDFC Bank results', snippet='Indian company', url='https://example.com/article',
                     source='test', relevance_score=0)])
    result = SearchService._filter_ranked_news_for_context(response, log_scope='test')
    assert result.results == []


def test_partial_cashflow_is_retained_and_same_period_fallback_fills_holes():
    from data_provider.base import DataFetcherManager as Manager
    def block(report):
        return {'status': 'partial', 'data': {'financial_report': report},
                'missing_fields': ['revenue'], 'source_chain': [], 'errors': []}
    native = block({'report_date': '2026-06-30', 'operating_cash_flow': 123, 'revenue': None})
    extra = {'status': 'ok', 'data': {'dividend': {'ttm_dividend_yield_pct': 3}}}
    merged = Manager._merge_native_fundamental_block('earnings', native, extra)
    assert merged['data']['financial_report']['operating_cash_flow'] == 123
    extra = block({'report_date': '2026-06-30', 'revenue': 456})
    assert Manager._merge_native_fundamental_block('earnings', native, extra)['data']['financial_report']['revenue'] == 456
    extra['data']['financial_report']['report_date'] = '2025-12-31'
    assert Manager._merge_native_fundamental_block('earnings', native, extra)['data']['financial_report']['revenue'] is None


def test_partial_native_blocks_survive_stage_timeout(monkeypatch):
    from data_provider.base import DataFetcherManager
    cfg = SimpleNamespace(tushare_token='test-only', enable_fundamental_pipeline=True,
                          fundamental_cache_ttl_seconds=0, fundamental_stage_timeout_seconds=1,
                          fundamental_fetch_timeout_seconds=1, fundamental_retry_max=0)
    def native(*a, on_block, **kw):
        on_block('valuation', {'status': 'ok', 'data': {'pe_ratio': 5}})
        return None
    manager = DataFetcherManager(fetchers=[SimpleNamespace(name='TushareFetcher', priority=0, get_fundamental_snapshot=native)])
    monkeypatch.setattr(manager, '_run_with_timeout', lambda fn, *a, **kw: (fn(), 'timeout', 1))
    monkeypatch.setattr(manager, '_get_legacy_fundamental_context', lambda *a, **kw: {})
    monkeypatch.setattr('src.config.get_config', lambda: cfg)
    assert manager.get_fundamental_context('601166')['valuation']['data']['pe_ratio'] == 5


def test_missing_fundamentals_do_not_imply_low_risk():
    from src.services.screening.models import Pick
    from src.services.screening.risk import apply_risk_overlay
    pick = Pick(rank=1, code='601166', name='Test', price=10, amount=1e9,
                pe_ratio=5, pb_ratio=0.5, final_score=80, screen_score=80)
    picks, notes = apply_risk_overlay([pick])
    assert picks[0].risk_level == 'unknown'
    assert any('数据覆盖不足' in flag for flag in picks[0].risk_flags)


def test_ranking_preserves_unreviewed_score_and_passes_dated_facts(monkeypatch):
    import json
    from src.services.screening.models import Pick
    from src.services.screening import ranker
    picks = [Pick(rank=i+1, code=code, name='Test', screen_score=80, final_score=80)
             for i, code in enumerate(('600001', '600002', '600003'))]
    picks[0].dsa_context = {'fundamentals': {'growth': {'status': 'ok', 'data': {'roe': 0, 'report_date': '2026-06-30'},
                                                     'source_chain': [{'provider': 'tushare:fina_indicator'}]}}}
    prompt = ranker._format_dsa_context_for_prompt(picks[0])
    assert '2026-06-30' in prompt and 'tushare:fina_indicator' in prompt and '"roe":0' in prompt
    monkeypatch.setattr(ranker, '_call_llm', lambda *a, **kw: json.dumps({'ranked': [{'code': p.code, 'llm_score': 70} for p in picks[:2]]}))
    ranked = ranker.rank_candidates_with_metadata(picks, '', 'test', 'test', max_retries=0)
    unseen = next(p for p in ranked.picks if p.code == '600003')
    assert unseen.llm_score is None and unseen.final_score == 80


def test_deepseek_short_ranking_disables_thinking_only_on_verified_endpoint():
    from src.services.screening.ranker import _apply_screening_litellm_generation_params as params
    request = {'model': 'openai/deepseek-v4-flash', 'api_base': 'https://api.deepseek.com/v1'}
    assert params(request, model=request['model'], temperature=0.2)['extra_body']['thinking']['type'] == 'disabled'
    request['api_base'] = 'https://example.com/v1'
    assert 'extra_body' not in params(request, model=request['model'], temperature=0.2)
    routes = [{'model_name': 'ranking', 'litellm_params': {'model': 'openai/deepseek-v4-flash', 'api_base': 'https://api.deepseek.com/v1'}}]
    assert params({'model': 'ranking'}, model='ranking', temperature=0.2, model_list=routes)['extra_body']['thinking']['type'] == 'disabled'
    routes.append({'model_name': 'ranking', 'litellm_params': {'model': 'openai/deepseek-v4-flash', 'api_base': 'https://other.example/v1'}})
    assert 'extra_body' not in params({'model': 'ranking'}, model='ranking', temperature=0.2, model_list=routes)


def test_adapter_keeps_cashflow_when_indicator_and_income_fail():
    from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter
    tables = _tables()
    tables['fina_indicator'] = []
    tables['income'] = []
    result = TushareFundamentalAdapter(lambda api, **kw: pd.DataFrame(tables[api])).fetch('601166', budget_seconds=5, as_of='2026-10-12')
    assert result['earnings']['data']['financial_report']['operating_cash_flow'] == 20
    assert result['earnings']['data']['financial_report']['report_date'] == '2026-06-30'


def test_valuation_metadata_shell_does_not_block_usable_fallback():
    from data_provider.base import DataFetcherManager as Manager
    native = {'status': 'partial', 'data': {'pe_ratio': None, 'as_of': '2026-10-09', 'currency': 'CNY'}}
    fallback = {'status': 'ok', 'data': {'pe_ratio': 5, 'pb_ratio': 0.5}}
    merged = Manager._merge_native_fundamental_block('valuation', native, fallback)
    assert merged['data']['pe_ratio'] == 5


def test_snapshot_industry_supplement_preserves_existing_values_and_dates(monkeypatch):
    from src.services.screening import snapshot
    monkeypatch.setenv('TUSHARE_TOKEN', 'test-only')
    monkeypatch.setattr(snapshot, '_INDUSTRY_CACHE', {'expires': float('inf'), 'mapping': {'600001': '银行', '600002': '医药'}, 'fetched_at': '2026-10-12'})
    frame = pd.DataFrame({'code': ['600001', '600002'], 'industry': ['原行业', ''], 'MAX_TRADE_DATE': ['2026-10-09']*2})
    snapshot._supplement_snapshot_industry(frame)
    snapshot._annotate_snapshot_data_dates(frame)
    assert frame.industry.tolist() == ['原行业', '医药']
    assert frame.attrs['snapshot_data_dates']['MAX_TRADE_DATE'] == '2026-10-09'
    assert frame.attrs['data_warnings']
