"""Scope, date, unit and failure-isolation regressions for extended facts."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest


def test_dividends_count_implemented_only_once_and_preserve_per_share_units():
    from data_provider.tushare_fact_normalizers import dividend_facts
    rows = [dict(end_date='20251231', ann_date='20260327', div_proc=stage,
                 cash_div_tax=0.5, ex_date=ex, imp_ann_date=imp)
            for stage, ex, imp in [('预案', None, None), ('实施', '20260612', '20260608'),
                                    ('实施', '20260612', '20260608'), ('实施', '20261020', '20261018')]]
    rows += [dict(end_date='20260630', ann_date='20260901', div_proc='预案', cash_div_tax=2)]
    result = dividend_facts(pd.DataFrame(rows), '2026-10-12')
    assert result['ttm_cash_dividend_per_share'] == 0.5
    assert result['implemented_event_count_365d'] == 1
    assert result['cash_unit'] == 'CNY/share_before_tax'
    assert all(e.get('implementation_announcement_date') != '2026-10-18' for e in result['events'])
    assert any(e['stage'] == '预案' for e in result['events'])


def test_dividend_missing_cash_is_unknown_not_zero():
    from data_provider.tushare_fact_normalizers import dividend_facts
    frame = pd.DataFrame([dict(end_date='20251231', ann_date='20260601', div_proc='实施',
                               ex_date='20260612', cash_div_tax=None)])
    assert dividend_facts(frame, '2026-10-12')['ttm_cash_dividend_per_share'] is None


def test_holder_comparison_excludes_new_and_custodian_and_is_not_buy_volume():
    from data_provider.tushare_fact_normalizers import holder_facts
    frame = pd.DataFrame([
        dict(end_date=period, ann_date='20260828' if period == '20260630' else '20260430',
             holder_name=name, hold_amount=amount, holder_type=kind)
        for period, name, amount, kind in [
            ('20260630', '保险A', 120, '保险公司'), ('20260331', '保险A', 100, '保险公司'),
            ('20260630', '新基金', 500, '基金'),
            ('20260630', '香港中央结算有限公司', 400, '一般企业'),
            ('20260331', '香港中央结算有限公司', 200, '一般企业'),
            ('20260331', '退出前十', 90, '基金'),
        ]])
    result = holder_facts(frame)
    change = result['comparable_institution_change']
    assert change['change_shares'] == 20
    assert change['compared_holder_count'] == 1
    assert result['new_to_top10'] == ['新基金']
    assert result['absent_from_top10'] == ['退出前十']
    assert result['institution_holding_change'] is None
    assert change['is_transaction_flow'] is False


def test_industry_aggregation_counts_missing_and_does_not_mix_dates():
    from data_provider.tushare_fact_normalizers import industry_facts
    basics = pd.DataFrame({'ts_code': ['A', 'B', 'C'], 'industry': ['银行', '银行', '软件']})
    daily = pd.DataFrame({'ts_code': ['A', 'B', 'C'], 'trade_date': ['20261009']*3,
                          'pct_chg': [1, 3, -2]})
    flows = pd.DataFrame({'ts_code': ['A', 'B', 'C'], 'trade_date': ['20261009', '20261008', '20261009'],
                          'buy_lg_amount': [10, 999, 0], 'buy_elg_amount': [0, 0, 0],
                          'sell_lg_amount': [0, 0, 1], 'sell_elg_amount': [0, 0, 0]})
    prices, funds = industry_facts(basics, daily, flows, '20261009')
    bank = next(r for r in funds['top'] if r['name'] == '银行')
    assert bank['main_net_inflow'] == 100000
    assert bank['covered_count'] == 1 and bank['population_count'] == 2
    assert bank['coverage_ratio'] == 0.5
    assert funds['coverage_complete'] is False
    assert prices['top'][0]['change_pct'] == 2
    assert prices['definition'] == 'equal_weight_return_of_observed_traded_constituents'


def test_dragon_tiger_failed_day_not_negative_and_reasons_not_sessions():
    from data_provider.tushare_fact_normalizers import dragon_tiger_facts
    tables = {'20261009': pd.DataFrame({'ts_code': ['601166.SH']*2, 'trade_date': ['20261009']*2,
                                       'reason': ['涨幅', '换手'], 'net_amount': [10, 10]}),
              '20261008': None}
    result = dragon_tiger_facts('601166.SH', ['20261009', '20261008'], tables)
    assert result['recent_count'] == 1
    assert result['event_row_count'] == 2
    assert result['coverage_complete'] is False
    assert result['missing_sessions'] == ['2026-10-08']
    result = dragon_tiger_facts('600000.SH', ['20261009', '20261008'], tables)
    assert result['is_on_list'] is None
    assert result['recent_count'] is None


def test_news_exception_continues_to_other_provider():
    from src.search_service import SearchService, SearchResult, SearchResponse
    service = SearchService(bocha_keys=['dummy'], searxng_public_instances_enabled=False)
    good = SearchResponse(query='test', success=True, provider='good', results=[SearchResult(
        title='兴业银行 601166 最新公司公告', snippet='兴业银行披露季度报告',
        url='https://www.cib.com.cn/news/20261012.html', source='cib.com.cn',
        published_date=datetime.now().date().isoformat())])
    failing = SimpleNamespace(name='bad', is_available=True, search=Mock(side_effect=TimeoutError('private payload')))
    succeeding = SimpleNamespace(name='good', is_available=True, search=Mock(return_value=good))
    service._providers = [failing, succeeding]
    result = service.search_stock_news('601166', '兴业银行', max_results=3)
    assert result.results and succeeding.search.call_count == 1


def test_prompt_carries_extended_scope_and_dates():
    from src.services.screening.models import Pick
    from src.services.screening.ranker import _format_dsa_context_for_prompt
    pick = Pick(rank=1, code='601166', name='银行', final_score=80, screen_score=80)
    pick.dsa_context = {'fundamentals': {
        'earnings': {'status': 'ok', 'data': {'dividend': {'cash_unit': 'CNY/share_before_tax', 'ttm_cash_dividend_per_share': 0.5}}},
        'institution': {'status': 'partial', 'data': {'comparable_institution_change': {'change_shares': 20, 'is_transaction_flow': False}}},
        'dragon_tiger': {'status': 'partial', 'data': {'missing_sessions': ['2026-10-08'], 'recent_count': None}},
    }}
    prompt = _format_dsa_context_for_prompt(pick)
    assert 'CNY/share_before_tax' in prompt
    assert 'is_transaction_flow' in prompt and '2026-10-08' in prompt


def test_agent_capital_flow_uses_integrated_facts_with_provenance(monkeypatch):
    from src.agent.tools import data_tools
    native = {'status': 'partial', 'data': {'stock_flow': {'main_net_inflow': 10, 'as_of': '2026-10-09', 'unit': 'CNY'},
              'sector_rankings': {'definition': 'own_aggregate', 'coverage_complete': False}},
              'missing_fields': ['industry_constituent_coverage'], 'source_chain': [{'provider': 'tushare'}]}
    manager = SimpleNamespace(get_fundamental_context=Mock(return_value={'capital_flow': native}),
                              get_capital_flow_context=Mock(side_effect=AssertionError('legacy bypass')))
    monkeypatch.setattr(data_tools, '_get_fetcher_manager', lambda: manager)
    result = data_tools._handle_get_capital_flow('601166')
    assert result['main_net_inflow'] == 10
    assert result['as_of'] == '2026-10-09'
    assert result['sector_rankings']['definition'] == 'own_aggregate'
    assert result['missing_fields'] == ['industry_constituent_coverage']


def test_invalid_dividend_schema_never_becomes_confirmed_zero():
    from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter
    def query(api, **kwargs):
        if api == 'dividend':
            return pd.DataFrame([{'ts_code': '601166.SH', 'unrecognized': 'bad'}])
        return pd.DataFrame()
    result = TushareFundamentalAdapter(query).fetch('601166', budget_seconds=10, as_of='2026-10-12')
    assert 'dividend_events' in result['earnings']['missing_fields']
    assert result['earnings']['data'].get('dividend', {}).get('ttm_cash_dividend_per_share') is None


def _extended_query(api, **params):
    symbol = params.get('ts_code', '601166.SH')
    if api == 'stock_basic':
        return pd.DataFrame([{'ts_code': symbol, 'industry': '银行', 'name': '银行'}])
    if api == 'daily_basic':
        return pd.DataFrame([{'ts_code': symbol, 'trade_date': '20261009', 'pe_ttm': 5, 'pb': 1, 'total_mv': 10, 'circ_mv': 10, 'dv_ttm': 2}])
    if api == 'daily':
        return pd.DataFrame([{'ts_code': symbol, 'trade_date': params['trade_date'], 'pct_chg': 1}])
    if api == 'moneyflow':
        return pd.DataFrame([{'ts_code': symbol, 'trade_date': '20261009', 'buy_lg_amount': 1,
                              'buy_elg_amount': 0, 'sell_lg_amount': 0, 'sell_elg_amount': 0}])
    if api == 'dividend':
        return pd.DataFrame(columns=['ts_code', 'ann_date', 'end_date', 'div_proc', 'cash_div_tax', 'ex_date'])
    if api == 'trade_cal':
        return pd.DataFrame({'cal_date': ['20261009', '20261008', '20260930', '20260929', '20260928'], 'is_open': [1]*5})
    if api == 'top_list':
        return pd.DataFrame([{'ts_code': '600999.SH', 'trade_date': params['trade_date'], 'reason': '涨幅', 'net_amount': 10}]) if not params.get('ts_code') else pd.DataFrame()
    return pd.DataFrame()


def test_native_market_tables_shared_and_returned_payloads_isolated(monkeypatch):
    from data_provider.tushare_fetcher import TushareFetcher
    monkeypatch.setattr(TushareFetcher, '_init_api', lambda self: None)
    monkeypatch.setattr(TushareFetcher, '_determine_priority', lambda self: -1)
    fetcher = TushareFetcher()
    fetcher._api = SimpleNamespace(query=Mock(side_effect=_extended_query))
    first = fetcher.get_fundamental_snapshot('601166', budget_seconds=10)
    assert first['dragon_tiger']['status'] == 'ok'
    assert len(first['dragon_tiger']['data']['checked_sessions']) == 5
    first['boards']['data']['sector_rankings']['top'][0]['change_pct'] = 999
    second = fetcher.get_fundamental_snapshot('601166', budget_seconds=10)
    assert second['boards']['data']['sector_rankings']['top'][0]['change_pct'] == 1
    market_calls = [call for call in fetcher._api.query.call_args_list if not call.kwargs.get('ts_code')]
    assert len(market_calls) == 9  # 3 industry tables + calendar + 5 daily leaderboards


def test_market_cache_rejects_rate_limit_sleep(monkeypatch):
    import time
    from data_provider.tushare_fetcher import TushareFetcher
    monkeypatch.setattr(TushareFetcher, '_init_api', lambda self: None)
    monkeypatch.setattr(TushareFetcher, '_determine_priority', lambda self: -1)
    fetcher = TushareFetcher(rate_limit_per_minute=1)
    fetcher._minute_start = time.time()
    fetcher._call_count = 1
    fetcher._api = SimpleNamespace(query=Mock(side_effect=AssertionError('must not call')))
    fetcher._check_rate_limit = Mock(side_effect=AssertionError('must not sleep'))
    result = fetcher.get_fundamental_snapshot('601166', budget_seconds=0.1)
    assert result['valuation']['status'] == 'failed'
    fetcher._check_rate_limit.assert_not_called()


def test_nested_rankings_survive_history_and_market_structure_extraction():
    from src.utils.data_processing import extract_board_detail_fields
    rankings = {'top': [{'name': '银行', 'change_pct': 1, 'coverage_ratio': 0.5, 'as_of': '2026-10-09'}],
                'bottom': [], 'definition': 'own_equal_weight', 'as_of': '2026-10-09',
                'coverage_complete': False, 'membership_is_point_in_time': False}
    context = {'boards': {'status': 'partial', 'data': {'industry': '银行', 'sector_rankings': rankings}},
               'belong_boards': [{'name': '银行'}]}
    result = extract_board_detail_fields({'fundamental_context': context})
    assert result['sector_rankings']['top'][0]['name'] == '银行'
    assert result['sector_rankings']['top'][0]['coverage_ratio'] == 0.5
    assert result['sector_rankings']['definition'] == 'own_equal_weight'
    assert result['sector_rankings']['coverage_complete'] is False


def test_revised_dividend_amount_replaces_same_event_instead_of_doubling():
    from data_provider.tushare_fact_normalizers import dividend_facts
    rows = [dict(end_date='20251231', ann_date=ann, imp_ann_date=ann, div_proc='实施',
                 cash_div_tax=cash, ex_date='20260612') for ann, cash in [('20260601', 0.4), ('20260608', 0.5)]]
    result = dividend_facts(pd.DataFrame(rows), '2026-10-12')
    assert result['ttm_cash_dividend_per_share'] == 0.5
    assert result['ttm_event_count'] == 1


def test_unknown_dividend_publication_date_never_confirms_zero():
    from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter
    def query(api, **params):
        if api == 'dividend':
            return pd.DataFrame([dict(ts_code='601166.SH', end_date='20251231', ann_date=None,
                                     div_proc='实施', ex_date='20260612', cash_div_tax=0.5)])
        return pd.DataFrame()
    result = TushareFundamentalAdapter(query).fetch('601166', budget_seconds=10, as_of='2026-10-12')
    div = result['earnings']['data']['dividend']
    assert div['ttm_cash_dividend_per_share'] is None
    assert div['events_complete'] is False


def test_industry_mixed_date_daily_table_not_marked_complete():
    from data_provider.tushare_fact_normalizers import industry_facts
    basics = pd.DataFrame({'ts_code': ['A', 'B'], 'industry': ['银行', '银行']})
    daily = pd.DataFrame({'ts_code': ['A', 'B'], 'trade_date': ['20261009', '20261008'], 'pct_chg': [1, 2]})
    price, _ = industry_facts(basics, daily, pd.DataFrame(), '20261009')
    assert price['coverage_complete'] is False
    assert price['rejected_daily_rows'] == 1


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_standard_analysis_prompt_preserves_distinct_provider_yield_and_event_basis(language):
    from unittest.mock import patch
    from src.analyzer import GeminiAnalyzer
    with patch.object(GeminiAnalyzer, '_init_litellm', return_value=None):
        analyzer = GeminiAnalyzer(use_legacy_default_prompt=True)
    context = {'code': '601166', 'fundamental_context': {
        'earnings': {'data': {'dividend': {'ttm_dividend_yield_pct': 4, 'ttm_cash_dividend_per_share': 1,
                     'definition': 'provider_dv_ttm_do_not_recompute', 'as_of': '2026-10-09', 'price_basis': 'daily_close'}}},
        'capital_flow': {'status': 'partial', 'data': {'stock_flow': {'main_net_inflow': 1},
                        'sector_rankings': {'top': [{'name': 'Banks'}], 'definition': 'own_aggregate', 'coverage_complete': False}}},
        'institution': {'status': 'partial', 'data': {'comparable_institution_change': {'is_transaction_flow': False}}},
        'dragon_tiger': {'status': 'ok', 'data': {'lookback_sessions': 5}},
    }}
    prompt = analyzer._format_prompt(context, 'Bank', report_language=language)
    assert 'provider_dv_ttm_do_not_recompute' in prompt
    assert '2026-10-09' in prompt and 'daily_close' in prompt
    assert 'own_aggregate' in prompt and 'is_transaction_flow' in prompt and 'lookback_sessions' in prompt
    assert 'TTM cash dividend per share / current price' not in prompt
    assert '近12个月每股现金分红 / 当前价格' not in prompt


def test_public_sector_rankings_fall_back_to_labeled_own_aggregate(monkeypatch):
    from data_provider.tushare_fetcher import TushareFetcher
    monkeypatch.setattr(TushareFetcher, '_init_api', lambda self: None)
    monkeypatch.setattr(TushareFetcher, '_determine_priority', lambda self: -1)
    fetcher = TushareFetcher()
    fetcher._api = SimpleNamespace()
    monkeypatch.setattr(fetcher, 'get_trade_time', lambda **kw: '20261009')
    def call(api, **params):
        if api.startswith('moneyflow_ind'):
            raise PermissionError('denied')
        return _extended_query(api, **params)
    monkeypatch.setattr(fetcher, '_call_api_with_rate_limit', call)
    top, bottom = fetcher.get_sector_rankings(1)
    assert top[0]['name'] == '银行'
    assert top[0]['as_of'] == '2026-10-09'
    assert top[0]['definition'] == 'equal_weight_return_of_observed_traded_constituents'
    assert top[0]['source'] == 'computed_from_tushare'


def test_dividend_report_shows_count_latest_implemented_date_and_separate_bases():
    from data_provider.tushare_fact_normalizers import dividend_facts
    from src.notification import NotificationService
    dividend = dividend_facts(pd.DataFrame([
        dict(end_date='20260630', ann_date='20260901', div_proc='预案', cash_div_tax=2),
        dict(end_date='20251231', ann_date='20260601', div_proc='实施', cash_div_tax=0.5, ex_date='20260612'),
    ]), '2026-10-12')
    dividend.update(ttm_dividend_yield_pct=4, as_of='2026-10-09')
    labels = {key: key for key in ('shareholder_return_heading', 'ttm_cash_dividend_label', 'ttm_event_count_label',
                                   'ttm_dividend_yield_label', 'latest_ex_dividend_label')}
    lines = []
    NotificationService.__new__(NotificationService)._append_shareholder_return(
        lines, {'dividend': dividend, 'financial_report': {'currency': 'CNY'}}, labels)
    report = '\n'.join(lines)
    assert '| 1 |' in report and '2026-06-12' in report
    assert '2026-10-09' in report and 'Different definitions' in report
