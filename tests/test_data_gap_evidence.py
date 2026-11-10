from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.base import DataFetcherManager
from data_provider.realtime_types import ChipDistribution, get_chip_circuit_breaker
from data_provider.tushare_fetcher import TushareFetcher


def test_sina_concept_snapshot_has_unknown_date_and_filters_nan():
    fetcher = AkshareFetcher.__new__(AkshareFetcher)
    response = Mock(text='var S_Finance_bankuai_class = {"gn_a":"gn_a,机器人,10,1,0,2.5,1,2,x,1,1,1,甲", "gn_b":"gn_b,坏数据,1,1,1,NaN,1,1,x,1,1,1,乙"};')
    with patch("data_provider.akshare_fetcher.requests.get", return_value=response):
        top, bottom = fetcher.get_sina_concept_rankings(5)
    assert len(top) == 1
    assert top[0]["source"] == "sina"
    assert top[0]["as_of"] is None
    assert top[0]["updated_at"]
    assert "日期未提供" in top[0]["definition"]


def test_manager_uses_sina_route_and_preserves_cache_acquisition_time():
    DataFetcherManager.clear_concept_rankings_cache_for_tests()
    fetcher = Mock(name="fetcher", priority=1)
    fetcher.name = "AkshareFetcher"
    fetcher.get_sina_concept_rankings.return_value = ([{"name": "机器人", "change_pct": 2.5, "source": "sina", "updated_at": "original", "as_of": None}], [])
    manager = DataFetcherManager(fetchers=[fetcher])
    first = manager.get_concept_rankings()
    second = manager.get_concept_rankings()
    assert first == second
    assert first[0][0]["updated_at"] == "original"
    fetcher.get_concept_rankings.assert_not_called()


def test_chip_failure_metadata_distinguishes_permission_and_network():
    get_chip_circuit_breaker().reset()
    fetcher = Mock(priority=1)
    fetcher.name = "TushareFetcher"
    fetcher.get_chip_distribution.side_effect = PermissionError("no permission")
    manager = DataFetcherManager(fetchers=[fetcher])
    with patch("src.config.get_config", return_value=SimpleNamespace(enable_chip_distribution=True)):
        chip, evidence = manager.get_chip_distribution("601166", with_metadata=True)
    assert chip is None
    assert evidence["attempts"][0]["status"] == "no_permission"
    assert evidence["status"] == "unavailable"


def test_tushare_chip_uses_verified_response_date_and_correct_source():
    fetcher = TushareFetcher.__new__(TushareFetcher)
    rows = pd.DataFrame([{"trade_date": "20261009", "ts_code": "601166.SH", "price": 10., "percent": 100.}])
    with patch.object(fetcher, "get_trade_time", return_value="20261012"), patch.object(
        fetcher, "_call_api_with_rate_limit", side_effect=[rows, pd.DataFrame([{"close": 12.}])]
    ) as api:
        chip = fetcher.get_chip_distribution("601166")
    assert chip.source == "tushare"
    assert chip.date == "2026-10-09"
    assert api.call_args.kwargs["start_date"] == "20261009"


def test_chip_metadata_disabled_and_unsupported():
    manager = DataFetcherManager(fetchers=[])
    with patch("src.config.get_config", return_value=SimpleNamespace(enable_chip_distribution=False)):
        assert manager.get_chip_distribution("601166", with_metadata=True)[1]["status"] == "disabled"
    with patch("src.config.get_config", return_value=SimpleNamespace(enable_chip_distribution=True)):
        assert manager.get_chip_distribution("AAPL", with_metadata=True)[1]["status"] == "unsupported"


def test_chip_report_keeps_source_date_and_permission_reason():
    from src.analyzer import AnalysisResult, normalize_chip_structure_availability
    result = AnalysisResult(code='601166', name='兴业银行', sentiment_score=50,
                            trend_prediction='震荡', operation_advice='观望')
    normalize_chip_structure_availability(result, None, {
        'status': 'unavailable', 'attempts': [{'source': 'TushareFetcher', 'status': 'no_permission'}],
    })
    assert '权限不足' in result.dashboard['data_perspective']['chip_unavailable_reason']
    chip = ChipDistribution(code='601166', date='2026-10-09', source='tushare', avg_cost=18, profit_ratio=.5)
    normalize_chip_structure_availability(result, chip)
    note = result.dashboard['data_perspective']['chip_source_note']
    assert '2026-10-09' in note and 'tushare' in note and '估算' in note


@pytest.mark.parametrize('data_perspective', [None, [], 'unavailable'])
def test_chip_failure_evidence_normalizes_non_dict_perspective(data_perspective):
    from src.analyzer import AnalysisResult, normalize_chip_structure_availability
    result = AnalysisResult(code='601166', name='兴业银行', sentiment_score=50,
                            trend_prediction='震荡', operation_advice='观望',
                            dashboard={'data_perspective': data_perspective})
    evidence = {'status': 'unavailable', 'attempts': [
        {'source': 'TushareFetcher', 'status': 'no_permission'},
    ]}
    normalize_chip_structure_availability(result, None, evidence)
    dp = result.dashboard['data_perspective']
    assert dp['chip_evidence'] == evidence
    assert '权限不足' in dp['chip_unavailable_reason']
    assert dp.get('chip_structure', {}) == {}


@pytest.mark.parametrize('source,as_of', [('sina', None), ('sina', '2026-10-12'),
                                        ('eastmoney', '2000-01-01')])
def test_related_concepts_do_not_promote_unverified_snapshot_to_current_signal(source, as_of):
    from src.notification import NotificationService
    from src.report_language import get_report_labels
    service = NotificationService.__new__(NotificationService)
    lines = []
    service._append_related_boards(lines, {
        'belong_boards': [{'name': '机器人', 'type': '概念', 'source': 'eastmoney'}],
        'concept_top': [{'name': '机器人', 'change_pct': 2.5, 'source': source, 'as_of': as_of,
                         'updated_at': '2026-10-12T14:57:00Z', 'definition': '供应商分类，不同口径'}],
    }, get_report_labels('zh'))
    rendered = '\n'.join(lines)
    assert '领涨' not in rendered
    assert '背景' in rendered
    assert source in rendered
    assert (as_of or '未知') in rendered
    assert '不同口径' in rendered
