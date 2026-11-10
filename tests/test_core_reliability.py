"""Offline regressions for data, ranking and valuation reliability."""

import json
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from data_provider.base import RateLimitError
from data_provider.tickflow_fetcher import TickFlowFetcher
from src.services.portfolio_service import PortfolioService
from src.services.portfolio_risk_service import PortfolioRiskService
from src.services.screening import daily, snapshot
from src.services.screening.models import Pick
from src.services.screening import ranker


@pytest.mark.parametrize("custom_url", [None, "https://gateway.example.test/data"])
@pytest.mark.parametrize("entry", ["snapshot", "daily"])
def test_tushare_screening_uses_unified_post_endpoint(monkeypatch, custom_url, entry):
    monkeypatch.setenv("TUSHARE_TOKEN", "offline-test-token")
    monkeypatch.setenv("TUSHARE_TRADE_DATE", "20261009")
    monkeypatch.setenv("TUSHARE_DAILY_ADJ", "none")
    monkeypatch.delenv("TUSHARE_HTTP_URL", raising=False)
    monkeypatch.delenv("TUSHARE_API_URL", raising=False)
    if custom_url:
        monkeypatch.setenv("TUSHARE_API_URL", custom_url)
    tables = {
        "daily": {
            "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"],
            "items": [["600000.SH", "20261009", 10, 11, 9, 10, 100, 1000, 0]],
        },
        "daily_basic": {
            "fields": ["ts_code", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv"],
            "items": [["600000.SH", 1, 1.2, 8, 1, 20000, 18000]],
        },
        "stock_basic": {
            "fields": ["ts_code", "symbol", "name", "industry"],
            "items": [["600000.SH", "600000", "Test", "Bank"]],
        },
    }
    calls = []

    def post(url, **kwargs):
        body = kwargs["json"]
        calls.append((url, body, kwargs["timeout"]))
        return SimpleNamespace(status_code=200, text=json.dumps({"code": 0, "data": tables[body["api_name"]]}))

    monkeypatch.setattr("requests.post", post)
    result = snapshot._fetch_tushare() if entry == "snapshot" else daily._fetch_daily_tushare("600000", lookback_days=30)
    assert not result.empty
    assert calls
    assert all(url == (custom_url or "http://api.tushare.pro") for url, _, _ in calls)
    assert all("ts_type_name" not in body["params"] for _, body, _ in calls)
    assert all(timeout > 0 for _, _, timeout in calls)
    if entry == "snapshot":
        assert result.iloc[0]["amount"] == 1_000_000
        assert result.iloc[0]["total_mv"] == 200_000_000


def test_tickflow_prefetch_respects_symbol_limit_even_with_saved_batch_100(monkeypatch):
    fetcher = TickFlowFetcher("offline-batch-limit", batch_size=100)
    batches = []
    quotes = []
    client = SimpleNamespace(
        klines=SimpleNamespace(batch=lambda symbols, **kwargs: batches.append(list(symbols)) or {}),
        quotes=SimpleNamespace(get=lambda **kwargs: quotes.append(kwargs["symbols"]) or []),
    )
    monkeypatch.setattr(fetcher, "_get_client", lambda: client)
    symbols = [f"60000{i}" for i in range(10)] + ["600010"]
    fetcher.prefetch_daily_klines(symbols, start_date="2026-09-01", end_date="2026-10-09")
    fetcher.prefetch_realtime_quotes(symbols, batch_size=100)
    assert [len(group) for group in batches] == [5, 5, 1]
    assert [len(group) for group in quotes] == [5, 5, 1]


def test_tickflow_request_budget_shared_across_instances_and_recovers(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("data_provider.tickflow_fetcher.monotonic", lambda: now[0])
    first = TickFlowFetcher("offline-budget-shared")
    second = TickFlowFetcher("offline-budget-shared")
    for index in range(10):
        (first if index % 2 else second)._admit_request()
    with pytest.raises(RateLimitError):
        first._admit_request()
    now[0] += 60.01
    second._admit_request()


def test_tickflow_sdk_does_not_retry_outside_the_local_budget(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tickflow", SimpleNamespace(TickFlow=lambda **kwargs: calls.append(kwargs)))
    TickFlowFetcher("offline-sdk-retry")._build_client()
    assert calls[0]["max_retries"] == 0


def test_ranker_uses_final_json_not_think_draft():
    candidates = [Pick(rank=1, code="600000", name="Test", screen_score=80, final_score=80)]
    response = '<think>{"ranked":[{"code":"999999"}]}</think>{"ranked":[{"code":"600000","llm_score":90}]}'
    parsed = ranker._parse_ranking_response_detail(response, candidates)
    assert parsed.coverage == 1
    assert not any("unknown_code" in error for error in parsed.errors)


def test_ranker_rejects_transport_truncation_without_increasing_budget(monkeypatch):
    from src.config import Config
    monkeypatch.setattr('src.config.get_config', lambda: Config(free_model_enabled=False))
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length",
            message=SimpleNamespace(content='{"ranked":[{"code":"600000"}]}'),
        )])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    picks = [Pick(rank=1, code="600000", name="Test", screen_score=80, final_score=80)]
    result = ranker.rank_candidates_with_metadata(picks, "test", "offline-key", "openai/test", max_retries=0, max_tokens=512)
    assert not result.ranked
    assert result.failure_reason == "truncated_response"
    assert all(call["max_tokens"] == 512 for call in calls)
    assert picks[0].llm_score is None


def _portfolio_service(*, currency="CNY", close=None, cash=None):
    account = SimpleNamespace(id=1, name="Offline", owner_id=None, broker=None, market="cn", base_currency="CNY")
    trade = SimpleNamespace(id=1, symbol="600000", market="cn", currency=currency, trade_date=date(2026, 9, 1),
                            side="buy", quantity=10, price=10, fee=0, tax=0)
    repo = SimpleNamespace(
        list_accounts=lambda **kwargs: [account],
        list_trades=lambda *args, **kwargs: [trade],
        list_cash_ledger=lambda *args, **kwargs: cash or [],
        list_corporate_actions=lambda *args, **kwargs: [],
        get_latest_close_with_date=lambda **kwargs: (close, date(2026, 10, 9)) if close is not None else None,
        get_latest_fx_rate=lambda **kwargs: None,
    )
    return PortfolioService(repo=repo)


def test_portfolio_missing_price_marks_valuation_incomplete():
    service = _portfolio_service()
    result = service.get_portfolio_snapshot(as_of=date(2026, 10, 9), read_only=True)
    assert result["valuation_complete"] is False
    assert result["data_quality"] == "partial"
    assert "missing_price" in result["limitations"]
    assert result["accounts"][0]["positions"][0]["valuation_complete"] is False


def test_portfolio_missing_fx_never_converts_one_to_one():
    service = _portfolio_service(currency="USD", close=12)
    amount, stale, mode = service.convert_amount(amount=100, from_currency="USD", to_currency="CNY", as_of_date=date(2026, 10, 9))
    assert amount == 0
    assert stale and mode == "missing_fx"
    result = service.get_portfolio_snapshot(as_of=date(2026, 10, 9), read_only=True)
    assert result["valuation_complete"] is False
    assert "missing_fx" in result["limitations"]
    assert result["total_equity"] != 20


def test_portfolio_valid_price_and_same_currency_remain_complete():
    service = _portfolio_service(close=12)
    result = service.get_portfolio_snapshot(as_of=date(2026, 10, 9), read_only=True)
    assert result["valuation_complete"] is True
    assert result["total_equity"] == 20
    assert result["unrealized_pnl"] == 20


def test_portfolio_missing_price_does_not_create_stop_loss():
    service = _portfolio_service()
    snapshot_data = service.get_portfolio_snapshot(as_of=date(2026, 10, 9), read_only=True)
    result = PortfolioRiskService._build_stop_loss(snapshot_data, {"stop_loss_alert_pct": 10, "stop_loss_near_ratio": 0.8})
    assert not result["items"]


def test_portfolio_incomplete_valuation_has_no_concentration_conclusion():
    service = _portfolio_service()
    risk = PortfolioRiskService(repo=service.repo, portfolio_service=service, config=SimpleNamespace(), decision_signal_service=Mock())
    result = risk._build_concentration(service.get_portfolio_snapshot(as_of=date(2026, 10, 9), read_only=True), 35, as_of_date=date(2026, 10, 9))
    assert result["available"] is False
    assert result["reason"] == "incomplete_valuation"


@pytest.mark.parametrize("amount", [float("nan"), float("inf")])
def test_portfolio_nonfinite_amount_rejected(amount):
    service = _portfolio_service()
    with pytest.raises(ValueError):
        service.convert_amount(amount=amount, from_currency="CNY", to_currency="CNY", as_of_date=date(2026, 10, 9))


def test_agent_compact_snapshot_preserves_missing_valuation_not_fake_totals():
    from src.agent.tools.data_tools import _compact_portfolio_snapshot

    snapshot_data = _portfolio_service().get_portfolio_snapshot(as_of=date(2026, 10, 9), read_only=True)
    compact = _compact_portfolio_snapshot(snapshot_data)
    assert compact["valuation_complete"] is False
    assert compact["total_equity"] is None
    assert "missing_price" in compact["limitations"]
    assert compact["accounts"][0]["total_market_value"] is None


def test_drawdown_does_not_treat_incomplete_snapshot_as_zero_equity():
    row = SimpleNamespace(snapshot_date=date(2026, 10, 9), total_equity=0, base_currency="CNY", fx_stale=False,
                          payload=json.dumps({"valuation_complete": False}))
    repo = SimpleNamespace(list_daily_snapshots_for_risk=lambda **kwargs: [row])
    risk = PortfolioRiskService(repo=repo, portfolio_service=_portfolio_service(), config=SimpleNamespace(), decision_signal_service=Mock())
    result = risk._build_drawdown(account_id=1, as_of_date=date(2026, 10, 9), cost_method="fifo", threshold_pct=15, lookback_days=30)
    assert result["available"] is False
    assert result["max_drawdown_pct"] is None


@pytest.mark.parametrize("alert_type,block", [("portfolio_concentration", "concentration"), ("portfolio_drawdown", "drawdown"), ("portfolio_stop_loss", "stop_loss")])
def test_incomplete_risk_alert_is_degraded_not_normal(alert_type, block):
    from src.services.portfolio_alerts import PortfolioRiskAlert, evaluate_portfolio_risk_alert

    rule = PortfolioRiskAlert(target_scope="portfolio_account", target="1", alert_type=alert_type, parameters={})
    risk = SimpleNamespace(get_risk_report=lambda **kwargs: {block: {"available": False, "reason": "incomplete_valuation"}})
    result = evaluate_portfolio_risk_alert(rule, portfolio_service=_portfolio_service(), risk_service=risk)
    assert result["record_status"] == "degraded"
    assert result["observed_value"] is None
    assert not json.loads(result["diagnostics"])["data_available"]


@pytest.mark.parametrize("batch", [False, True])
def test_tickflow_real_sdk_has_no_hidden_names_or_internal_batch_requests(monkeypatch, tmp_path, batch):
    from tickflow._cache import InstrumentNameCache
    from tickflow.resources.klines import Klines

    calls = []
    bars = {"timestamp": [1791475200000], "open": [10], "high": [12], "low": [9],
            "close": [11], "volume": [100], "amount": [11000]}

    def get(path, params):
        calls.append(path)
        return {"data": {symbol: bars for symbol in params["symbols"].split(",")} if "symbols" in params else bars}

    def post(path, json):
        calls.append(path)
        return {"data": [{"symbol": symbol, "name": "Offline"} for symbol in json["symbols"]]}

    transport = SimpleNamespace(get=get, post=post)
    klines = Klines(transport, instrument_cache=InstrumentNameCache(cache_dir=str(tmp_path)))
    fetcher = TickFlowFetcher(f"offline-real-sdk-{batch}", batch_size=200, max_symbols_per_request=200, requests_per_minute=1)
    fetcher._client = SimpleNamespace(klines=klines)
    if batch:
        fetcher.prefetch_daily_klines([f"{600000 + index}" for index in range(101)], start_date="2026-10-01", end_date="2026-10-09")
    else:
        fetcher._fetch_raw_data("600000", "2026-10-01", "2026-10-09")
    assert len(calls) == 1
    assert "/v1/instruments" not in calls


def test_tickflow_thread_interleaving_does_not_release_unexpired_budget(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event, Lock

    monkeypatch.setattr(TickFlowFetcher, "_request_budget", {})
    first_sampled = Event()
    release_first = Event()
    counter_lock = Lock()
    count = [0]

    def clock():
        with counter_lock:
            count[0] += 1
            call = count[0]
        if call == 1:
            first_sampled.set()
            assert release_first.wait(2)
            return 100.0
        return 101.0

    monkeypatch.setattr("data_provider.tickflow_fetcher.monotonic", clock)
    fetcher = TickFlowFetcher("offline-thread-window", requests_per_minute=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetcher._admit_request)
        assert first_sampled.wait(2)
        second = pool.submit(fetcher._admit_request)
        try:
            second.result(timeout=0.1)
        except TimeoutError:
            pass
        finally:
            release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)
    monkeypatch.setattr("data_provider.tickflow_fetcher.monotonic", lambda: 160.01)
    fetcher._admit_request()
    with pytest.raises(RateLimitError):
        fetcher._admit_request()


def test_legacy_missing_price_snapshot_cannot_create_false_drawdown():
    rows = [
        SimpleNamespace(snapshot_date=date(2026, 10, 8), total_equity=100, base_currency="CNY", fx_stale=False,
                        payload=json.dumps({"positions": [{"price_available": True, "price_source": "history_close"}]})),
        SimpleNamespace(snapshot_date=date(2026, 10, 9), total_equity=0, base_currency="CNY", fx_stale=False,
                        payload=json.dumps({"positions": [{"price_available": False, "price_source": "missing"}]})),
    ]
    repo = SimpleNamespace(list_daily_snapshots_for_risk=lambda **kwargs: rows)
    risk = PortfolioRiskService(repo=repo, portfolio_service=_portfolio_service(), config=SimpleNamespace(), decision_signal_service=Mock())
    result = risk._build_drawdown(account_id=1, as_of_date=date(2026, 10, 9), cost_method="fifo", threshold_pct=15, lookback_days=30)
    assert result["available"] is False
    assert not result["alert"]
    assert result["max_drawdown_pct"] is None
