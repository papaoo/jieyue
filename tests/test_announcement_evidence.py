"""Issuer attribution is evidence, not a keyword hit in a newspaper page."""
from datetime import date
from unittest.mock import Mock, patch

import pytest

from src.search_service import SearchResult, SearchResponse, SearchService


def test_newspaper_full_page_cannot_be_company_evidence():
    item = SearchResult("信息披露 A03", "证券代码600000，向兴业银行601166申请授信。",
                        "https://epaper.stcn.com/att/20261012/A03.pdf", "证券时报")
    ranked = SearchService._rank_news_response(
        SearchResponse("test", [item], "test"), stock_code="601166", stock_name="兴业银行",
        prefer_chinese=True, max_results=5, log_scope="test",
    )
    response = SearchService._filter_ranked_news_for_context(ranked, log_scope="test")
    assert not response.results


def test_unverified_other_issuer_announcement_is_background():
    item = SearchResult("某科技股份有限公司关于授信的公告", "向兴业银行601166申请贷款。",
                        "https://static.cninfo.com.cn/finalpage/2026-10-12/a.pdf", "巨潮")
    scored = SearchService._score_news_relevance(item, stock_code="601166", stock_name="兴业银行")
    assert scored.relevance_category != SearchService._DIRECT_NEWS_CATEGORY


@pytest.mark.parametrize('title', [
    '某科技股份有限公司关于向兴业银行申请授信的公告',
    '某科技股份有限公司向兴业银行申请贷款的公告',
    '某科技：与兴业银行签订授信合同的公告',
])
def test_announcement_counterparty_in_headline_is_not_issuer(title):
    item = SearchResult(title, '601166 兴业银行提供授信。',
                        'https://static.cninfo.com.cn/finalpage/2026-10-12/a.pdf', '巨潮',
                        date.today().isoformat())
    scored = SearchService._score_news_relevance(item, stock_code='601166', stock_name='兴业银行')
    assert scored.relevance_category != SearchService._DIRECT_NEWS_CATEGORY


def test_verified_issuer_is_preserved_by_date_filter_and_scoring():
    item = SearchResult("关于回购股份的公告", "仅公告元数据，正文待核验。", "https://www.cninfo.com.cn/a",
                        "巨潮", date.today().isoformat(), issuer_code="601166", evidence_kind="announcement_metadata")
    service = SearchService()
    filtered = service._filter_news_response(SearchResponse("test", [item], "CNINFO"), search_days=3,
                                             max_results=5, log_scope="test")
    scored = service._score_news_relevance(filtered.results[0], stock_code="601166", stock_name="兴业银行")
    assert scored.issuer_code == "601166"
    assert scored.relevance_category == service._DIRECT_NEWS_CATEGORY
    assert item.url in scored.to_text()


def test_cninfo_parser_rejects_other_issuers_and_returns_single_document():
    from src.announcement_source import _cninfo
    responses = [
        {"stockList": [{"code": "601166", "orgId": "org-bank"}]},
        {"announcements": [
            {"secCode": "600000", "announcementTitle": "其他公司公告", "announcementId": "1"},
            {"secCode": "601166", "secName": "兴业银行", "announcementTitle": "关于回购股份的公告",
             "announcementId": "2", "announcementTime": 1791734400000,
             "adjunctUrl": "finalpage/2026-10-12/2.PDF"},
        ]},
    ]
    with patch("src.announcement_source.requests.get") as get, patch("src.announcement_source.requests.post") as post:
        get.return_value.json.return_value = responses[0]
        post.return_value.json.return_value = responses[1]
        result = _cninfo("601166", "2026-10-10", "2026-10-12")
    assert len(result) == 1
    assert result[0]["issuer_code"] == "601166"
    assert result[0]["url"].endswith("/2.PDF")
    assert result[0]["evidence_kind"] == "announcement_metadata"
    assert post.call_args.kwargs["timeout"]


def test_events_apply_freshness_and_admission():
    service = SearchService()
    provider = Mock(name="provider", is_available=True)
    provider.name = "Mock"
    provider.search.return_value = SearchResponse("test", [
        SearchResult("信息披露 A03", "兴业银行601166", "https://epaper.stcn.com/A03.pdf", "报纸", date.today().isoformat()),
        SearchResult("兴业银行业绩预告", "601166公告", "https://example.com/old", "test", "2018-01-01"),
    ], "Mock")
    service._providers = [provider]
    assert not service.search_stock_events("601166", "兴业银行").results
