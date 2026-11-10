"""Offline auxiliary producers preserve observed dates, scope and source evidence."""
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.services.research_delivery.auxiliary_producers import produce_auxiliary
from src.services.research_delivery.evidence_store import EvidenceStore


DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network/config/notification/LLM side effect")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("src.config.get_config", forbidden)
    monkeypatch.setattr("src.notification.NotificationService.__init__", forbidden)
    monkeypatch.setattr("src.services.etf_rotation_service.run_etf_rotation", forbidden)


def config(**kwargs):
    return SimpleNamespace(**dict(dict(stock_list=["600001", "AAPL", "hk00700"],
        etf_rotation_pool=["510300", "518880"], etf_rotation_safe_asset="511880",
        etf_rotation_lookback_days=5, etf_rotation_rebalance="weekly", etf_rotation_top_n=1,
        etf_rotation_switch_buffer_pct=3., etf_rotation_cost_bps=5.), **kwargs))


def run(tmp_path, category, **kwargs):
    return produce_auxiliary(category, business_date=DAY, config=kwargs.pop("config", config()),
                             evidence_dir=tmp_path, now=NOW, **kwargs)


class ETFManager:
    def __init__(self, damage=""):
        self.damage, self.calls = damage, []

    def get_daily_data(self, code, *, start_date, end_date):
        self.calls.append((code, start_date, end_date))
        dates = pd.bdate_range(end=DAY, periods=30)
        frame = pd.DataFrame({"date": dates, "close": [100 * 1.001 ** n for n in range(30)]})
        if self.damage == "short":
            frame = frame.tail(5)
        if code == "511880":
            if self.damage == "missing":
                frame.loc[frame.index[-1], "close"] = None
            if self.damage == "stale":
                frame = frame.iloc[:-1]
            if self.damage == "jump":
                frame.loc[frame.index[-1], "close"] *= 2
        if self.damage != "raw":
            frame.attrs["price_adjustment"] = "forward"
        return frame, "FixtureFetcher"


def test_etf_real_loader_report_typed_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.trading_calendar.get_trading_dates",
                        lambda market, start, end: pd.bdate_range(start, end))
    manager = ETFManager()
    result = run(tmp_path, "etf_rotation", fetcher_manager=manager)
    assert result.state in {"ready", "degraded"} and not result.blockers
    assert len(manager.calls) == 3
    assert all((DAY - date.fromisoformat(start)).days <= 366 and end == DAY.isoformat()
               for _, start, end in manager.calls)
    assert result.result["as_of"] == DAY.isoformat()
    assert result.result["target_weights"] and result.result["ranking"]
    assert result.result["execution_assumptions"]["price_adjustment"] == "forward"
    assert all(set(item) == {"id", "name", "metrics", "as_of", "source"} for item in result.result["items"])
    assert "## 回测表现" not in result.result["markdown"]
    assert result.evidence and result.evidence[0]["provenance"] == "provider_normalized"
    assert result.evidence[0]["source"] != "tushare"
    for evidence in result.evidence:
        assert sha256(Path(evidence["path"]).read_bytes()).hexdigest() == evidence["sha256"]
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.parametrize("damage", ["missing", "stale", "jump", "short", "raw"])
def test_etf_requires_every_asset_actual_quote_warmup_and_adjustment(tmp_path, monkeypatch, damage):
    monkeypatch.setattr("src.core.trading_calendar.get_trading_dates",
                        lambda market, start, end: pd.bdate_range(start, end))
    result = run(tmp_path, "etf_rotation", fetcher_manager=ETFManager(damage))
    assert result.state == "blocked" and result.blockers
    assert not result.result.get("target_weights")


@pytest.mark.parametrize("provider_path", ["injected_manager", "real_manager"])
def test_etf_provider_exception_logs_and_artifacts_are_safe(tmp_path, monkeypatch, caplog, provider_path):
    sentinel = "REVIEW_SENTINEL_NOT_A_REAL_SECRET"
    def broken(*args, **kwargs):
        raise RuntimeError("token=" + sentinel)
    if provider_path == "injected_manager":
        manager = SimpleNamespace(get_daily_data=broken)
    else:
        from data_provider import DataFetcherManager
        from data_provider.realtime_types import CircuitBreaker
        from data_provider.tencent_fetcher import TencentFetcher
        monkeypatch.setattr(DataFetcherManager, "_daily_source_health", CircuitBreaker())
        monkeypatch.setattr(TencentFetcher, "_fetch_raw_data", broken)
        manager = DataFetcherManager(fetchers=[TencentFetcher()])
    with caplog.at_level(logging.INFO):
        result = run(tmp_path, "etf_rotation", fetcher_manager=manager)
        logging.getLogger("auxiliary_test_unrelated").info("outside-boundary-diagnostic")
    assert result.state == "blocked" and result.blockers == ["etf_asset_data_unavailable"]
    assert sentinel not in caplog.text
    assert sentinel not in repr(result.to_dict())
    assert all(sentinel not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "source_request_failed" in caplog.text
    assert "outside-boundary-diagnostic" in caplog.text


def test_safe_provider_error_context_restores_and_isolates_threads(caplog):
    from concurrent.futures import ThreadPoolExecutor
    from data_provider.base import safe_exception_details, summarize_exception

    error = RuntimeError("normal-original-detail")
    class UnprintableError(Exception):
        def __str__(self):
            raise AssertionError("safe diagnostics must not stringify exceptions")
    def diagnostic():
        detail = summarize_exception(error)
        logging.getLogger("auxiliary_test_concurrent").warning("%s", detail[1])
        return detail
    with caplog.at_level(logging.WARNING):
        with safe_exception_details(enabled=True):
            assert summarize_exception(error) == ("source_error", "source_request_failed")
            assert summarize_exception(UnprintableError()) == ("source_error", "source_request_failed")
            with safe_exception_details():  # Nested defaults cannot downgrade a safe boundary.
                assert summarize_exception(error) == ("source_error", "source_request_failed")
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(diagnostic).result() == ("RuntimeError", "normal-original-detail")
        assert summarize_exception(error) == ("RuntimeError", "normal-original-detail")
        with pytest.raises(ValueError):
            with safe_exception_details(enabled=True):
                raise ValueError("context exit")
        assert summarize_exception(error) == ("RuntimeError", "normal-original-detail")
    assert "normal-original-detail" in caplog.text


@pytest.mark.parametrize("provider_name", ["efinance", "akshare"])
def test_etf_builtin_history_error_branch_uses_safe_request_context(tmp_path, monkeypatch, caplog, provider_name):
    from data_provider import DataFetcherManager
    from data_provider.realtime_types import CircuitBreaker
    sentinel = "REVIEW_BUILTIN_SENTINEL_NOT_A_REAL_SECRET"
    def broken(*args, **kwargs):
        raise RuntimeError("rate_limit token=" + sentinel)
    monkeypatch.setattr(DataFetcherManager, "_daily_source_health", CircuitBreaker())
    if provider_name == "efinance":
        from data_provider import efinance_fetcher as provider_module
        monkeypatch.setattr(provider_module, "get_config", lambda: SimpleNamespace(enable_eastmoney_patch=False))
        monkeypatch.setattr(provider_module, "_ef_call_with_timeout", broken)
        monkeypatch.setitem(sys.modules, "efinance", SimpleNamespace(stock=SimpleNamespace(get_quote_history=Mock())))
        provider = provider_module.EfinanceFetcher(sleep_min=0, sleep_max=0)
    else:
        from data_provider import akshare_fetcher as provider_module
        monkeypatch.setattr(provider_module, "get_config", lambda: SimpleNamespace(enable_eastmoney_patch=False))
        monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(fund_etf_hist_em=broken))
        provider = provider_module.AkshareFetcher(sleep_min=0, sleep_max=0)
    monkeypatch.setattr(provider, "_enforce_rate_limit", lambda: None)
    monkeypatch.setattr(provider, "_set_random_user_agent", lambda: None)
    with caplog.at_level(logging.INFO):
        result = run(tmp_path, "etf_rotation", fetcher_manager=DataFetcherManager(fetchers=[provider]))
    assert result.state == "blocked"
    assert sentinel not in caplog.text and sentinel not in repr(result.to_dict())
    assert "source_request_failed" in caplog.text
    assert all(sentinel not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    # The same provider's normal diagnostic contract returns immediately after exit.
    if provider_name == "efinance":
        _, message = provider._build_history_failure_message("510300", "20251015", "20261015",
            RuntimeError("normal-original-detail"), elapsed=0., is_etf=True)
        assert "normal-original-detail" in message
    else:
        from data_provider.base import RateLimitError
        monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(fund_etf_hist_em=Mock(
            side_effect=RuntimeError("rate_limit normal-original-detail"))))
        with pytest.raises(RateLimitError, match="normal-original-detail"):
            provider._fetch_etf_data("510300", "2025-10-15", "2026-10-15")
    assert sentinel not in caplog.text


def hotspot(**groups):
    return SimpleNamespace(get_hotspots=Mock(return_value=dict(dict(
        leading_industries=[], leading_concepts=[], lagging_themes=[]), **groups)))


def theme(as_of=DAY.isoformat(), source="industry", **kwargs):
    return dict(name="测试行业", code="BK01", change_pct=1.2, rank=1,
                as_of=as_of, source=source, provider="FixtureProvider", **kwargs)


def test_radar_never_relabels_undated_or_stale_rows(tmp_path):
    service = hotspot(leading_industries=[theme(as_of=None), theme(as_of="2026-10-14")])
    result = run(tmp_path, "market_radar", hotspot_service=service)
    assert result.state == "blocked" and not result.result.get("items")
    assert {"industry_rankings_unavailable", "concept_rankings_unavailable"} <= set(result.warnings)
    service.get_hotspots.assert_called_once_with(market="cn", limit=5)


def test_radar_partial_only_verified_group_and_missing_concepts(tmp_path):
    service = hotspot(leading_industries=[theme()], leading_concepts=[theme(as_of=None, source="concept")])
    result = run(tmp_path, "market_radar", hotspot_service=service)
    assert result.state == "degraded" and not result.blockers
    assert len(result.result["items"]) == 1
    assert "concept_rankings_unavailable" in result.warnings
    assert result.result["items"][0]["metrics"] == {"change_pct": 1.2, "rank": 1}
    assert "strength_score" not in repr(result.result)


def snapshot_context(tmp_path):
    table = pd.DataFrame([
        dict(ts_code="600001.SH", trade_date="20261015", close=10., pct_chg=1., amount=2.),
        dict(ts_code="600002.SH", trade_date="20261015", close=10., pct_chg=-1., amount=3.),
        dict(ts_code="600003.SH", trade_date="20261015", close=10., pct_chg=0., amount=4.),
    ])
    entry = EvidenceStore(tmp_path).capture("daily", {"trade_date": "20261015"}, table,
                                           fetched_at=NOW, business_date=DAY)
    batch = dict(category_id="dual_low", business_date=DAY.isoformat(), state="degraded", blockers=[],
        coverage=dict(daily_response_coverage=1., daily_response_count=3, independent_universe_count=3),
        evidence=[entry], result={})
    return {"stock_batches": [batch]}


def test_radar_hash_verified_daily_breadth_is_not_concept_or_full_market(tmp_path):
    result = run(tmp_path, "market_radar", hotspot_service=hotspot(), collector_context=snapshot_context(tmp_path))
    assert result.state == "degraded"
    item = result.result["items"][0]
    assert item["id"] == "market:breadth"
    assert item["metrics"] == dict(advancers=1, decliners=1, flat=1, observed_count=3, turnover_yuan=9000.)
    assert result.coverage["scope"] == "observed_snapshot_not_full_market"
    assert {"industry_rankings_unavailable", "concept_rankings_unavailable"} <= set(result.warnings)


@pytest.mark.parametrize("damage", ["hash", "path", "coverage", "date", "blocked", "row_date", "params"])
def test_radar_rejects_untrusted_snapshot_context(tmp_path, damage):
    context = snapshot_context(tmp_path)
    batch = context["stock_batches"][0]
    entry = batch["evidence"][0]
    if damage == "hash":
        Path(entry["path"]).write_text("{}", encoding="utf-8")
    elif damage == "path":
        entry["path"] = str(tmp_path.parent / "outside.json")
    elif damage == "coverage":
        batch["coverage"]["daily_response_coverage"] = .94
    elif damage == "date":
        batch["business_date"] = "2026-10-14"
    elif damage == "blocked":
        batch["blockers"] = ["history_invalid"]
    else:
        path = Path(entry["path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        if damage == "row_date":
            data["rows"][0][1] = "20261014"
        else:
            data["params"]["ts_code"] = "600001.SH"
        raw = json.dumps(data).encode("utf-8")
        path.write_bytes(raw)
        entry["sha256"] = sha256(raw).hexdigest()
    result = run(tmp_path, "market_radar", hotspot_service=hotspot(), collector_context=context)
    assert result.state == "blocked"


def announcement(code="600001", **kwargs):
    return dict(dict(issuer_code=code, title="关于回购股份的公告", published_date="2026-10-15",
        source="CNINFO", url="https://static.cninfo.com.cn/finalpage/2026-10-15/a.pdf",
        evidence_kind="announcement_metadata"), **kwargs)


def envelope(records=(), **kwargs):
    return dict(dict(ok=True, records=list(records), source="CNINFO", fetched_at=NOW.isoformat()), **kwargs)


def test_announcements_explicit_scope_date_issuer_and_public_url(tmp_path):
    fetch = Mock(return_value=envelope([
        announcement(), announcement("600002"), announcement(published_date="2026-10-12"),
        announcement(published_date="2026-10-16"), announcement(url="http://127.0.0.1/x"),
        announcement(url="https://static.cninfo.com.cn/a.pdf?token=secret"),
    ]))
    result = run(tmp_path, "announcement_risk", announcement_fetcher=fetch)
    assert result.state == "degraded" and len(result.result["records"]) == 1
    assert result.result["records"][0]["issuer_code"] == "600001"
    assert result.coverage["scanned_count"] == 1 and result.coverage["unscanned_count"] == 0
    assert "secret" not in repr(result.to_dict())
    fetch.assert_called_once_with("600001", days=3, limit=10, as_of=DAY)


def test_announcements_names_only_from_exact_verified_reference(tmp_path):
    context = snapshot_context(tmp_path)
    entry = EvidenceStore(tmp_path).capture("stock_basic", dict(exchange="", list_status="L"),
        pd.DataFrame([dict(ts_code="600001.SH", name="精确公司"), dict(ts_code="600002.SH", name="其他公司"),
                      dict(ts_code="600003.SH", name="第三公司")]), fetched_at=NOW, business_date=DAY)
    context["stock_batches"][0]["evidence"].append(entry)
    result = run(tmp_path, "announcement_risk", collector_context=context,
        announcement_fetcher=Mock(return_value=envelope([announcement()])))
    assert result.result["records"][0]["issuer_name"] == "精确公司"
    assert set(result.result["research_context"]["instruments"]) == {"600001"}
    assert result.result["research_context"]["instruments"]["600001"]["source"] == "tushare.stock_basic"
    stored = json.loads(Path(result.evidence[-1]["path"]).read_text(encoding="utf-8"))
    assert stored["payload"]["records"][0]["issuer_name"] == "精确公司"


def test_etf_names_resolved_before_freeze_and_unknown_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.trading_calendar.get_trading_dates", lambda market, start, end: pd.bdate_range(start, end))
    manager = ETFManager()
    manager.get_stock_name = Mock(side_effect=lambda code, allow_realtime: "沪深300ETF" if code == "510300" else None)
    result = run(tmp_path, "etf_rotation", fetcher_manager=manager)
    assert not result.blockers
    assert manager.get_stock_name.call_count == 3
    assert all(call.kwargs == {"allow_realtime": False} for call in manager.get_stock_name.call_args_list)
    named = {row["id"]: row for row in result.result["items"]}
    assert named["510300"]["name"] == "沪深300ETF"
    assert named["518880"]["name"] is None
    assert result.result["execution_assumptions"]["lookback_days"] == 5
    labels = result.result["research_context"]["instruments"]
    assert labels["510300"]["asset_type"] == "etf"
    assert labels["518880"]["status"] == "unknown"
    assert labels["510300"]["ts_code"] == "510300.SH"
    assert labels["510300"]["market"] == "cn" and labels["510300"]["exchange"] == "SH"
    assert labels["510300"]["instrument_id"] == "cn:SH:etf:510300"
    stored = [json.loads(Path(e["path"]).read_text(encoding="utf-8")) for e in result.evidence]
    assert any(row.get("payload", {}).get("instruments") == labels for row in stored)


def test_etf_identity_uses_existing_exchange_mapping_and_rejects_unsupported_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.trading_calendar.get_trading_dates", lambda market, start, end: pd.bdate_range(start, end))
    manager = ETFManager()
    result = run(tmp_path, "etf_rotation", fetcher_manager=manager,
        config=config(etf_rotation_pool=["159915", "510300"], etf_rotation_safe_asset=""))
    assert not result.blockers
    labels = result.result["research_context"]["instruments"]
    assert labels["159915"]["ts_code"] == "159915.SZ"
    assert labels["159915"]["instrument_id"] == "cn:SZ:etf:159915"
    assert labels["159915"]["identity_source"] == "stock_list_parser._classify_bare_code"
    assert {r["code"] for r in result.result["ranking"]} == {"159915", "510300"}
    invalid = ETFManager()
    rejected = run(tmp_path, "etf_rotation", fetcher_manager=invalid,
        config=config(etf_rotation_pool=["100001"], etf_rotation_safe_asset=""))
    assert rejected.blockers == ["etf_pool_invalid"]
    assert not invalid.calls


@pytest.mark.parametrize("failure_layer", ["manager", "tencent"])
def test_etf_name_lookup_failure_keeps_debug_logs_and_evidence_safe(tmp_path, monkeypatch, caplog, failure_layer):
    from data_provider import DataFetcherManager
    from data_provider.realtime_types import CircuitBreaker
    from data_provider.tencent_fetcher import TencentFetcher
    sentinel = "NAME_LOOKUP_NOT_A_REAL_SECRET"
    def fail(*args, **kwargs):
        raise RuntimeError("credential=" + sentinel)
    provider = TencentFetcher()
    manager = ETFManager()
    monkeypatch.setattr(DataFetcherManager, "_daily_source_health", CircuitBreaker())
    monkeypatch.setattr(provider, "get_daily_data", lambda stock_code, start_date, end_date, days=30: manager.get_daily_data(
        stock_code, start_date=start_date, end_date=end_date)[0])
    if failure_layer == "manager":
        monkeypatch.setattr(provider, "get_stock_name", fail)
    else:
        monkeypatch.setattr("data_provider.tencent_fetcher.requests.get", fail)
    monkeypatch.setattr("src.core.trading_calendar.get_trading_dates", lambda market, start, end: pd.bdate_range(start, end))
    with caplog.at_level(logging.DEBUG):
        result = run(tmp_path, "etf_rotation", fetcher_manager=DataFetcherManager(fetchers=[provider]),
            config=config(etf_rotation_pool=["159998", "159997"], etf_rotation_safe_asset="159996"))
    assert not result.blockers
    assert "etf_instrument_names_unknown" in result.warnings
    assert sentinel not in caplog.text and sentinel not in repr(result.to_dict())
    assert "source_request_failed" in caplog.text
    assert all(sentinel not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))


def test_existing_name_manager_false_still_resolves_provider_and_normal_debug_is_preserved(monkeypatch, caplog):
    from data_provider import DataFetcherManager
    from data_provider.tencent_fetcher import TencentFetcher
    provider = TencentFetcher()
    resolve = Mock(return_value="测试ETF")
    monkeypatch.setattr(provider, "get_stock_name", resolve)
    manager = DataFetcherManager(fetchers=[provider])
    assert manager.get_stock_name("159995", allow_realtime=False) == "测试ETF"
    resolve.assert_called_once_with("159995")
    resolve.side_effect = RuntimeError("normal-name-lookup-diagnostic")
    with caplog.at_level(logging.DEBUG):
        assert not manager.get_stock_name("159994", allow_realtime=False)
    assert "normal-name-lookup-diagnostic" in caplog.text


def test_etf_name_lookup_is_bounded_without_changing_asset_ranking(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.trading_calendar.get_trading_dates", lambda market, start, end: pd.bdate_range(start, end))
    manager = ETFManager()
    manager.get_stock_name = Mock(return_value="测试ETF")
    codes = [str(159900 + i) for i in range(31)]
    result = run(tmp_path, "etf_rotation", fetcher_manager=manager,
        config=config(etf_rotation_pool=codes, etf_rotation_safe_asset=""))
    assert not result.blockers and len(result.result["ranking"]) == 31
    assert manager.get_stock_name.call_count == 30
    assert result.result["research_context"]["instruments"][codes[-1]]["status"] == "unknown"
    assert "etf_name_lookup_limit_reached" in result.warnings


def test_radar_uses_verified_shared_industry_without_etf_manager(tmp_path, monkeypatch):
    context = snapshot_context(tmp_path)
    entry = EvidenceStore(tmp_path).capture("stock_basic", dict(exchange="", list_status="L"),
        pd.DataFrame([dict(ts_code=f"{600001+i}.SH", name=f"公司{i}", industry="银行") for i in range(3)]),
        fetched_at=NOW, business_date=DAY)
    context["stock_batches"][0]["evidence"].append(entry)
    monkeypatch.setattr("src.services.research_delivery.auxiliary_producers._manager", lambda: pytest.fail("ETF manager in radar"))
    result = run(tmp_path, "market_radar", collector_context=context)
    assert not result.blockers and result.coverage["industry_available"]
    industries = [row for row in result.result["items"] if row["id"].startswith("industry:")]
    assert industries[0]["name"] == "银行"
    assert industries[0]["source"] == "computed_from_tushare"
    assert {field: industries[0]["metrics"][field] for field in ("advancers", "decliners", "flat", "unknown_count")} == dict(
        advancers=1, decliners=1, flat=1, unknown_count=0)
    assert result.result["research_context"]["industries"]["membership_is_point_in_time"] is False
    assert "hotspot_source_failed" not in result.warnings


def test_announcements_partial_failure_bounded_empty_sample_and_scope(tmp_path):
    fetch = Mock(side_effect=[envelope(), RuntimeError("token=do-not-record")])
    result = run(tmp_path, "announcement_risk", config=config(stock_list=["600001", "600002"]), announcement_fetcher=fetch)
    assert result.state == "degraded" and not result.blockers
    assert result.coverage["scanned_count"] == 2 and result.coverage["failed_count"] == 1
    assert result.result["records"] == []
    assert "bounded_sample_not_no_company_risk" in result.warnings
    assert "do-not-record" not in repr(result)
    empty = run(tmp_path, "announcement_risk", config=config(stock_list=[]), announcement_fetcher=fetch)
    assert empty.state == "blocked" and "announcement_scope_empty" in empty.blockers
    codes = [f"{600001 + n:06d}" for n in range(35)]
    fetch = Mock(return_value=envelope())
    capped = run(tmp_path, "announcement_risk", config=config(stock_list=codes), announcement_fetcher=fetch)
    assert fetch.call_count == 30 and capped.coverage["unscanned_count"] == 5


def test_announcements_stale_fetch_rejected(tmp_path):
    result = run(tmp_path, "announcement_risk", announcement_fetcher=Mock(return_value=
        envelope([announcement()], fetched_at="2026-10-14T18:00:00+08:00")))
    assert result.state == "blocked" and not result.result.get("records")
    assert result.coverage["scanned_count"] == 1 and result.coverage["failed_count"] == 1


def test_announcements_use_refreshed_clock_after_real_fetch(tmp_path):
    later = NOW + timedelta(seconds=1)
    calls = []
    def clock():
        calls.append(1)
        return NOW if len(calls) == 1 else later
    result = produce_auxiliary("announcement_risk", business_date=DAY, config=config(), evidence_dir=tmp_path,
        now=clock, announcement_fetcher=Mock(return_value=envelope([announcement()], fetched_at=later.isoformat())))
    assert result.state == "degraded" and len(result.result["records"]) == 1
    assert result.evidence[0]["fetched_at"] == later.isoformat()


def test_announcements_normalized_scope_candidates_and_private_fields_not_read(tmp_path):
    context = {"stock_batches": [dict(business_date=DAY.isoformat(), state="degraded", blockers=[],
        result=dict(picks=[dict(code="600002", name="候选")]), coverage={}, evidence=[]),
        dict(business_date="2026-10-14", state="ready", blockers=[], result=dict(picks=[dict(code="600003")]))],
        "holdings": [{"code": "600004", "cost": 123456}]}
    fetch = Mock(return_value=envelope())
    result = run(tmp_path, "announcement_risk", config=config(stock_list=["sh600001", "600001.SH", "AAPL"]),
                 collector_context=context, announcement_fetcher=fetch)
    assert result.coverage["scanned_codes"] == ["600001", "600002"]
    assert [call.args[0] for call in fetch.call_args_list] == ["600001", "600002"]
    assert "123456" not in repr(result.to_dict())


def test_announcement_existing_fetcher_explicit_date_anchor_preserves_issuer_filter(monkeypatch):
    from src import announcement_source
    fetch = Mock(return_value=[announcement(published_date="2026-10-13"), announcement(published_date="2026-10-16")])
    monkeypatch.setattr(announcement_source, "_eastmoney", fetch)
    monkeypatch.setattr(announcement_source, "_cninfo", Mock(return_value=[]))
    result = announcement_source.fetch_company_announcements("600001", as_of=DAY)
    fetch.assert_called_once_with("600001", "2026-10-13", "2026-10-15")
    assert len(result["records"]) == 1 and result["records"][0]["published_date"] == "2026-10-13"


@pytest.mark.parametrize("category", ["etf_rotation", "market_radar", "announcement_risk"])
def test_progress_interruption_propagates_for_worker_fencing(tmp_path, category):
    class Cancelled(Exception):
        pass
    def stop(payload):
        raise Cancelled("stop")
    with pytest.raises(Cancelled):
        run(tmp_path, category, progress=stop)


def test_unsupported_and_wrong_business_day_do_not_fetch(tmp_path):
    assert run(tmp_path, "system_status").blockers == ["unsupported_auxiliary_category"]
    fetch = Mock()
    result = produce_auxiliary("announcement_risk", business_date="2026-10-14", config=config(),
        evidence_dir=tmp_path, announcement_fetcher=fetch, now=NOW)
    assert result.state == "blocked" and not fetch.called


def test_generic_capture_rejects_credentials_and_records_provenance(tmp_path):
    store = EvidenceStore(tmp_path)
    with pytest.raises(ValueError):
        store.capture_payload("fixture", "prices", {"token": "private"},
            fetched_at=NOW, business_date=DAY, provenance="provider_normalized")
    entry = store.capture_payload("fixture", "prices", {"close": 10.},
        fetched_at=NOW, business_date=DAY, provenance="provider_normalized")
    assert entry["source"] == "fixture" and entry["provenance"] == "provider_normalized"


def test_default_etf_and_radar_do_not_load_singleton_config(tmp_path, monkeypatch):
    from data_provider.tencent_fetcher import TencentFetcher
    manager = ETFManager()
    def daily(self, stock_code, start_date, end_date, days=30):
        return manager.get_daily_data(stock_code, start_date=start_date, end_date=end_date)[0]
    monkeypatch.setattr(TencentFetcher, "get_daily_data", daily)
    monkeypatch.setattr(TencentFetcher, "get_stock_name", lambda self, code: None)
    monkeypatch.setattr("src.core.trading_calendar.get_trading_dates",
                        lambda market, start, end: pd.bdate_range(start, end))
    etf = run(tmp_path, "etf_rotation")
    assert not etf.blockers
    assert "etf_default_single_tencent_provider" in etf.warnings
    radar = run(tmp_path, "market_radar", collector_context=snapshot_context(tmp_path))
    assert radar.state == "degraded" and not radar.blockers
    assert radar.result["items"][0]["id"] == "market:breadth"
