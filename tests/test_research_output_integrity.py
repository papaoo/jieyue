"""Regressions from the 2026-10-12 live research-output audit (no network)."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from data_provider.tushare_fetcher import TushareFetcher
from src.analyzer import GeminiAnalyzer
from src.core.pipeline import StockAnalysisPipeline
from src.stock_analyzer import TrendAnalysisResult


def _context():
    return {
        'code': '601166', 'date': '2026-10-09',
        'today': {'date': '2026-10-09', 'close': 18.05, 'volume': 100000000},
        'yesterday': {'date': '2026-10-08', 'close': 18.24, 'volume': 200000000},
        'volume_change_ratio': 0.5,
    }


def _enhance(context, quote):
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(report_language='zh')
    pipeline.search_service = SimpleNamespace(news_window_days=3)
    trend = TrendAnalysisResult(code='601166', ma5=18, ma10=17.9, ma20=17.8)
    with patch('src.core.pipeline.get_market_now', return_value=datetime(2026, 10, 12, 14)):
        return pipeline._enhance_context(context, quote, None, trend,
                                         fundamental_context={}, market_phase_context={'is_partial_bar': True})


def _quote(**overrides):
    fields = dict(code='601166', price=18.31, pre_close=18.05, change_pct=1.44,
                  open_price=18.08, high=18.35, low=18.06, volume=64380000,
                  amount=1176000000, source=RealtimeSource.TUSHARE,
                  provider_timestamp='2026-10-12T13:57:00+08:00')
    fields.update(overrides)
    return UnifiedRealtimeQuote(**fields)


def test_legacy_tushare_volume_is_already_shares():
    fetcher = TushareFetcher.__new__(TushareFetcher)
    fetcher._api = MagicMock()
    fetcher._api.quotation.side_effect = RuntimeError('not supported')
    row = dict(name='兴业银行', price='18.31', pre_close='18.05', volume='64380000',
               amount='1176000000', high='18.35', low='18.06', open='18.08',
               date='2026-10-12', time='13:57:00')
    with patch.object(fetcher, '_check_rate_limit'), patch('tushare.get_realtime_quotes', return_value=pd.DataFrame([row])):
        quote = fetcher.get_realtime_quote('601166')
    assert quote.volume == 64380000
    assert quote.provider_timestamp == '2026-10-12T13:57:00+08:00'


def test_intraday_context_rolls_previous_session_without_mutating_input():
    original = _context()
    enhanced = _enhance(original, _quote())
    assert original['yesterday']['date'] == '2026-10-08'
    assert enhanced['yesterday']['date'] == '2026-10-09'
    assert enhanced['today']['pre_close'] == 18.05
    assert enhanced['price_change_ratio'] == 1.44
    assert enhanced['volume_change_ratio'] == 0.64
    assert enhanced['volume_comparison_basis'] == 'intraday_vs_previous_full_session'


def test_market_snapshot_uses_consistent_quote_base_and_units():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    snapshot = analyzer._build_market_snapshot(_enhance(_context(), _quote()))
    assert snapshot['prev_close'] == '18.05'
    assert snapshot['pct_chg'] == '1.44%'
    assert snapshot['change_amount'] == '0.26'
    assert snapshot['volume'] == '6438.00 万股'
    assert snapshot['is_partial_bar'] is True
    assert snapshot['provider_timestamp'] == '2026-10-12T13:57:00+08:00'


def test_market_snapshot_does_not_mix_daily_with_realtime_when_no_override():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    context = _context()
    context['realtime'] = _quote().to_dict()
    snapshot = analyzer._build_market_snapshot(context)
    assert snapshot['date'] == '2026-10-09'
    assert snapshot.get('price', snapshot['close']) == '18.05'
    assert snapshot.get('source') != 'tushare'


def test_quote_prior_session_is_not_redated_to_today():
    enhanced = _enhance(_context(), _quote(provider_timestamp='2026-10-09T15:00:00+08:00'))
    assert enhanced['today']['date'] == '2026-10-09'


def test_agent_trend_discloses_daily_date_and_source():
    from src.agent.tools.analysis_tools import _handle_analyze_trend
    frame = pd.DataFrame({'date': pd.bdate_range('2026-08-03', periods=50),
                          'close': 18., 'open': 18., 'high': 18.1, 'low': 17.9, 'volume': 1000000})
    frame.attrs['data_source'] = 'db_cache'
    with patch('src.agent.tools.analysis_tools._fetch_trend_data', return_value=frame):
        result = _handle_analyze_trend('601166')
    assert result['as_of'] == '2026-10-09'
    assert result['price_basis'] == 'daily_close'
    assert result['source'] == 'db_cache'


@pytest.mark.parametrize('provider', ['akshare', 'efinance'])
def test_cn_daily_volume_normalization_preserves_foreign_shares(provider):
    if provider == 'akshare':
        from data_provider.akshare_fetcher import AkshareFetcher as Fetcher
    else:
        from data_provider.efinance_fetcher import EfinanceFetcher as Fetcher
    fetcher = Fetcher.__new__(Fetcher)
    raw = pd.DataFrame({'日期': ['2026-10-09'], '收盘': [18.05], '成交量': [643800.]})
    assert fetcher._normalize_data(raw, '601166')['volume'].iloc[0] == 64380000
    assert fetcher._normalize_data(raw, 'AAPL')['volume'].iloc[0] == 643800
    if provider == 'akshare':
        raw.attrs['volume_unit'] = 'shares'  # Sina daily path already returns shares.
        assert fetcher._normalize_data(raw, '601166')['volume'].iloc[0] == 643800


def test_augment_does_not_stamp_old_quote_as_current_session():
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(enable_realtime_technical_indicators=True)
    frame = pd.DataFrame({'date': [pd.Timestamp('2026-10-09')], 'close': [18.05]})
    with patch('src.core.pipeline.get_market_now', return_value=datetime(2026, 10, 12, 14)), \
            patch('src.core.pipeline.is_market_open', return_value=True):
        result = pipeline._augment_historical_with_realtime(frame, _quote(provider_timestamp='2026-10-09T15:00:00+08:00'), '601166')
    assert len(result) == 1
    assert result['close'].iloc[0] == 18.05


def test_sector_report_and_hotspots_retain_date_and_no_invented_acceleration():
    from src.market_analyzer import MarketAnalyzer, MarketOverview
    from src.services.market_hotspot_service import MarketHotspotService
    rows = [{'name': '出版业', 'change_pct': 6.05, 'as_of': '2026-10-09',
             'source': 'computed_from_tushare', 'definition': 'equal_weight_mean_pct_chg'}]
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    with patch.object(analyzer, '_get_review_language', return_value='zh'):
        table = analyzer._build_sector_block(MarketOverview(date='2026-10-12', top_sectors=rows))
    assert '2026-10-09' in table
    assert 'computed_from_tushare' in table
    assert '2026-10-09' in analyzer._format_ranking_summary(rows)
    hotspots = MarketHotspotService(fetcher_manager=MagicMock()).get_hotspots(
        market='cn', trade_date='2026-10-12', sector_rankings={'top': rows, 'bottom': []}, concept_rankings={'top': [], 'bottom': []})
    assert hotspots['active_themes'][0]['as_of'] == '2026-10-09'
    assert hotspots['active_themes'][0]['phase'] == 'unknown'
    assert hotspots['trade_date'] == '2026-10-09'
    from src.services.market_structure_service import MarketStructureService
    from src.schemas.market_structure import StockBoardPosition
    service = MarketStructureService.__new__(MarketStructureService)
    theme, matched = service._infer_primary_theme(
        hotspots, [StockBoardPosition(name='出版业', change_pct=6.05, source='industry')])
    assert matched
    assert theme.phase == 'unknown'
    assert theme.as_of == '2026-10-09'


def test_end_of_day_breadth_uses_daily_trade_date_not_realtime_wildcards():
    fetcher = TushareFetcher.__new__(TushareFetcher)
    fetcher._api = MagicMock()
    def query(api, **kwargs):
        if api == 'daily':
            if kwargs != {'trade_date': '20261012'}:
                return pd.DataFrame()
            return pd.DataFrame([{'ts_code': '601166.SH', 'close': 18.44, 'pre_close': 18.05, 'amount': 1876000}])
        return pd.DataFrame([{'ts_code': '601166.SH', 'name': '兴业银行'}])
    with patch.object(fetcher, '_get_china_now', return_value=datetime(2026, 10, 12, 18)), \
            patch.object(fetcher, '_get_trade_dates', return_value=['20261012']), \
            patch.object(fetcher, '_call_api_with_rate_limit', side_effect=query):
        result = fetcher.get_market_stats()
    assert result is not None
    assert result['up_count'] == 1
    assert result['total_amount'] == pytest.approx(18.76)
    assert result['as_of'] == '2026-10-12'


@pytest.mark.parametrize('timestamp,stale', [(None, False), ('invalid', False),
    ('2026-10-08T15:00:00+08:00', False), ('2026-10-12T13:57:00+08:00', True)])
def test_unverified_quote_never_replaces_newer_context_or_creates_bar(timestamp, stale):
    quote = _quote(provider_timestamp=timestamp, is_stale=stale)
    context = _context()
    enhanced = _enhance(context, quote)
    assert enhanced['today'] == context['today']
    assert enhanced['date'] == context['date']
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(enable_realtime_technical_indicators=True)
    frame = pd.DataFrame({'date': [pd.Timestamp('2026-10-09')], 'close': [18.05]})
    with patch('src.core.pipeline.get_market_now', return_value=datetime(2026, 10, 12, 14)), \
            patch('src.core.pipeline.is_market_open', return_value=True):
        pd.testing.assert_frame_equal(pipeline._augment_historical_with_realtime(frame, quote, '601166'), frame)


def test_markdown_snapshot_exposes_business_date_and_partial_price():
    from src.notification import NotificationService
    renderer = NotificationService.__new__(NotificationService)
    snapshot = GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot(_enhance(_context(), _quote()))
    lines = []
    with patch.object(renderer, '_get_report_language', return_value='zh'):
        renderer._append_market_snapshot(lines, SimpleNamespace(market_snapshot=snapshot))
    markdown = '\n'.join(lines)
    assert '2026-10-12T13:57:00+08:00' in markdown
    assert '最新价' in markdown
    assert '盘中' in markdown
    assert 'provider_timestamp' in markdown
    assert '| 收盘 |' not in markdown


def test_breadth_metadata_reaches_report_and_payload():
    from src.market_analyzer import MarketAnalyzer, MarketOverview
    from src.core.market_profile import get_profile
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    analyzer.region = 'cn'
    analyzer.profile = get_profile('cn')
    analyzer.data_manager = MagicMock()
    analyzer.data_manager.get_market_stats.return_value = dict(up_count=1, source='tushare.daily', as_of='2026-10-09')
    overview = MarketOverview(date='2026-10-12')
    analyzer._get_market_statistics(overview)
    with patch.object(analyzer, '_get_review_language', return_value='zh'), \
            patch.object(analyzer, '_get_output_language', return_value='zh'):
        assert '2026-10-09' in analyzer._build_stats_block(overview)
        payload = analyzer.build_market_review_payload(overview, [], '# Test')
    assert payload['statistics_metadata']['as_of'] == '2026-10-09'


@pytest.mark.parametrize('renderer', ['history', 'template'])
def test_web_history_and_template_keep_snapshot_date_and_price_basis(renderer):
    from src.analyzer import AnalysisResult
    from src.report_language import get_report_labels
    from src.services.history_service import HistoryService
    from src.services.report_renderer import render
    from pathlib import Path
    snapshot = GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot(_enhance(_context(), _quote()))
    result = AnalysisResult(code='601166', name='兴业银行', sentiment_score=50,
                            trend_prediction='震荡', operation_advice='观望',
                            market_snapshot=snapshot, current_price=999)
    if renderer == 'history':
        lines = []
        HistoryService._append_market_snapshot_to_report(lines, result, get_report_labels('zh'))
        output = '\n'.join(lines)
    else:
        with patch('src.services.report_renderer._resolve_templates_dir', return_value=Path(__file__).resolve().parents[1] / 'templates'):
            output = render('markdown', [result], report_date='2026-10-12')
    assert '2026-10-12T13:57:00+08:00' in output
    assert '最新价' in output
    assert '盘中' in output
    assert '999' not in output  # Snapshot price, never a different result-level quote.
