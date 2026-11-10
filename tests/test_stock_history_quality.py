"""Deterministic history-service and HTTP JSON quality-boundary tests."""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient as Client
import pandas as pd
import pytest

from api.v1.endpoints.stocks import router
from api.v1.schemas.stocks import StockHistoryResponse
from src.services.stock_service import StockService


def bar(**updates):
    row = {
        "date": pd.Timestamp("2026-10-08"),
        "open": 4.0, "high": 4.2, "low": 3.9, "close": 4.1,
        "volume": 100.0, "amount": 410.0, "pct_chg": 0.0,
    }
    row.update(updates)
    return row


@pytest.fixture
def install_manager(monkeypatch):
    """Keep the real service and route; replace only DB/network boundaries."""
    monkeypatch.setattr("src.services.stock_service.StockRepository", MagicMock())

    def install(rows=None, *, error=None, source="TencentFetcher"):
        manager = MagicMock()
        manager.get_stock_name.return_value = "公开测试 ETF"
        if error:
            manager.get_daily_data.side_effect = error
        else:
            manager.get_daily_data.return_value = (pd.DataFrame(rows or []), source)
        monkeypatch.setattr("data_provider.base.DataFetcherManager", lambda: manager)
        return manager

    return install


def history_json():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/stocks")
    with Client(app) as client:
        response = client.get("/api/v1/stocks/510300/history?period=daily&days=30")
    assert response.status_code == 200, response.text
    assert "NaN" not in response.text and "Infinity" not in response.text
    return response.json()


def test_complete_history_preserves_zero_source_and_completion_time(install_manager):
    manager = install_manager([bar(volume=0.0, amount=0.0, pct_chg=0.0)])
    before = datetime.now(timezone.utc)
    payload = StockService().get_history_data("510300")
    after = datetime.now(timezone.utc)
    assert payload["status"] == "available"
    assert payload["source"] == "TencentFetcher"
    assert payload["adjustment"] is None
    assert payload["data"][0]["volume"] == 0.0
    assert payload["data"][0]["amount"] == 0.0
    assert payload["data"][0]["change_percent"] == 0.0
    completed = datetime.fromisoformat(payload["fetched_at"])
    assert completed.utcoffset().total_seconds() == 0
    assert before <= completed <= after
    assert {"daily-not-realtime", "adjustment-unknown", "freshness-not-verified"} <= set(payload["warnings"])
    manager.get_daily_data.assert_called_once_with("510300", days=30)
    StockHistoryResponse.model_validate(payload)
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), None])
def test_optional_nonfinite_values_become_null_at_real_http_boundary(install_manager, bad_value):
    install_manager([bar(volume=bad_value, amount=bad_value, pct_chg=bad_value)])
    payload = history_json()
    assert payload["status"] == "partial"
    assert payload["source"] == "TencentFetcher"
    assert payload["data"][0]["volume"] is None
    assert payload["data"][0]["amount"] is None
    assert payload["data"][0]["change_percent"] is None
    assert "optional-fields-missing:amount,change_percent,volume" in payload["warnings"]
    json.dumps(payload, allow_nan=False)


def test_zero_survives_real_http_boundary(install_manager):
    install_manager([bar(volume=0, amount=0, pct_chg=0)])
    payload = history_json()
    assert payload["status"] == "available"
    assert payload["data"][0]["volume"] == 0
    assert payload["data"][0]["amount"] == 0
    assert payload["data"][0]["change_percent"] == 0


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), None, 0.0, -1.0])
def test_invalid_required_price_row_is_skipped_not_zero_filled(install_manager, field, bad_value):
    install_manager([bar(**{field: bad_value}), bar(date=pd.Timestamp("2026-10-09"))])
    payload = StockService().get_history_data("510300")
    assert payload["status"] == "partial"
    assert len(payload["data"]) == 1
    assert payload["data"][0]["date"] == "2026-10-09"
    assert "invalid-ohlc-rows-skipped:1" in payload["warnings"]
    StockHistoryResponse.model_validate(payload)
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("updates", [
    {"open": 4.3}, {"close": 4.3}, {"low": 4.05},
    {"high": 4.05}, {"low": 4.3, "high": 3.8},
])
def test_invalid_price_envelope_is_rejected_at_real_http_boundary(install_manager, updates):
    install_manager([bar(**updates), bar(date=pd.Timestamp("2026-10-09"))])
    payload = history_json()
    assert payload["status"] == "partial"
    assert len(payload["data"]) == 1
    assert payload["data"][0]["date"] == "2026-10-09"
    assert "invalid-ohlc-rows-skipped:1" in payload["warnings"]


def test_positive_flat_price_bar_is_valid_and_optional_zero_is_preserved(install_manager):
    install_manager([bar(open=4, high=4, low=4, close=4, volume=0, amount=0, pct_chg=0)])
    payload = history_json()
    assert payload["status"] == "available"
    assert payload["data"][0] == {
        "date": "2026-10-08", "open": 4.0, "high": 4.0, "low": 4.0, "close": 4.0,
        "volume": 0.0, "amount": 0.0, "change_percent": 0.0,
    }


def test_all_invalid_rows_remain_partial_not_empty_market(install_manager):
    install_manager([bar(close=float("nan"))])
    payload = history_json()
    assert payload["data"] == []
    assert payload["status"] == "partial"
    assert "invalid-ohlc-rows-skipped:1" in payload["warnings"]


def test_upstream_empty_is_explicit_not_available(install_manager):
    manager = install_manager([])
    payload = history_json()
    assert payload["data"] == []
    assert payload["status"] == "empty"
    assert payload["source"] == "TencentFetcher"
    assert "empty-result" in payload["warnings"]
    assert payload["fetched_at"]
    manager.get_stock_name.assert_not_called()


@pytest.mark.parametrize("error", [RuntimeError("upstream unavailable"), ImportError("adapter unavailable")])
def test_source_failure_is_unavailable_with_no_invented_bars(install_manager, error):
    install_manager(error=error)
    payload = history_json()
    assert payload["data"] == []
    assert payload["status"] == "unavailable"
    assert payload["source"] is None
    assert "source-unavailable" in payload["warnings"]
    assert payload["fetched_at"]


def test_history_schema_accepts_legacy_payload_without_quality_metadata():
    payload = StockHistoryResponse.model_validate({"stock_code": "510300", "period": "daily", "data": []})
    assert payload.source is None
    assert payload.adjustment is None


def test_unsupported_period_keeps_existing_422_contract(install_manager):
    manager = install_manager([bar()])
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/stocks")
    with Client(app) as client:
        response = client.get("/api/v1/stocks/510300/history?period=weekly")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unsupported_period"
    manager.get_daily_data.assert_not_called()


def test_static_history_openapi_matches_real_router_contract():
    from api.app import create_app

    static_spec = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "architecture" / "api_spec.json").read_text(
            encoding="utf-8"
        )
    )
    runtime_spec = create_app().openapi()
    path = "/api/v1/stocks/{stock_code}/history"
    assert static_spec["paths"][path] == runtime_spec["paths"][path]
    assert static_spec["components"]["schemas"]["StockHistoryResponse"] == (
        runtime_spec["components"]["schemas"]["StockHistoryResponse"]
    )
