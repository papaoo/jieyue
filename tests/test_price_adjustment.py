# -*- coding: utf-8 -*-
"""价格复权口径一致性测试。

覆盖三件事：
1. Tushare 日线统一返回前复权（daily+adj_factor / fund_adj / hk_daily_adj）
2. 复权口径登记表（fetcher 声明 -> manager 登记 -> 落库）
3. ``stock_daily.price_adjustment`` 落库与混用检测
"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from data_provider.base import (
    VALID_PRICE_ADJUSTMENTS,
    price_adjustment_for_source,
    register_fetcher_price_adjustment,
)
from data_provider.tushare_fetcher import TushareFetcher
from src.storage import DatabaseManager


# ---------------------------------------------------------------- tushare 复权


def _raw_daily() -> pd.DataFrame:
    """两天原始不复权行情：第 2 天除权（pre_close 11 -> raw close 10）。"""
    return pd.DataFrame(
        {
            "ts_code": ["600519.SH"] * 2,
            "trade_date": ["20240102", "20240103"],
            "open": [11.0, 10.5],
            "high": [12.0, 11.0],
            "low": [10.5, 10.0],
            "close": [11.5, 10.5],
            "pre_close": [11.0, 11.5],
            "change": [0.5, -1.0],
            "pct_chg": [4.5455, -8.6957],
            "vol": [1000.0, 1200.0],
            "amount": [11500.0, 12600.0],
        }
    )


def _adj_factors() -> pd.DataFrame:
    """第 2 天发生 10 送 10 类除权：因子由 1.0 跳到 2.0。"""
    return pd.DataFrame(
        {
            "ts_code": ["600519.SH"] * 2,
            "trade_date": ["20240102", "20240103"],
            "adj_factor": [1.0, 2.0],
        }
    )


class TestTushareForwardAdjustment:
    def test_applies_forward_adjustment_anchored_at_latest_factor(self) -> None:
        out = TushareFetcher._apply_forward_adjustment(_raw_daily(), _adj_factors())

        # latest_factor=2.0 -> 第 1 天价格整体折半，第 2 天（最新）保持原值
        assert out["close"].tolist() == pytest.approx([5.75, 10.5])
        assert out["open"].tolist() == pytest.approx([5.5, 10.5])
        assert out["high"].tolist() == pytest.approx([6.0, 11.0])
        assert out["low"].tolist() == pytest.approx([5.25, 10.0])
        assert "adj_factor" not in out.columns

    def test_recomputes_change_and_pct_chg_from_adjusted_prices(self) -> None:
        out = TushareFetcher._apply_forward_adjustment(_raw_daily(), _adj_factors())

        # 原始 pct_chg 是按不复权价算的（-8.7%），复权后应按折后价重算
        assert out["change"].iloc[1] == pytest.approx(10.5 - 5.75)
        assert out["pct_chg"].iloc[1] == pytest.approx((10.5 / 5.75 - 1) * 100)
        assert pd.isna(out["pct_chg"].iloc[0])

    def test_ex_dividend_gap_is_removed_in_forward_adjusted_series(self) -> None:
        """10 送 10 除权：不复权序列出现 -50% 假跳空，前复权后应还原为真实涨跌。"""
        raw = pd.DataFrame(
            {
                "trade_date": ["20240102", "20240103", "20240104"],
                "open": [11.4, 5.80, 5.85],
                "high": [11.8, 5.95, 6.00],
                "low": [11.2, 5.70, 5.75],
                "close": [11.50, 5.75, 5.90],
                "pre_close": [11.0, 11.50, 5.75],
                "change": [0.5, -5.75, 0.15],
                "pct_chg": [4.55, -50.0, 2.61],
                "vol": [1000.0, 2000.0, 1500.0],
                "amount": [11500.0, 11500.0, 8850.0],
            }
        )
        factors = pd.DataFrame(
            {
                "ts_code": ["600519.SH"] * 3,
                "trade_date": ["20240102", "20240103", "20240104"],
                "adj_factor": [1.0, 2.0, 2.0],
            }
        )

        out = TushareFetcher._apply_forward_adjustment(raw, factors)

        # 除权日真实涨跌是 0%（平盘），不是不复权视角下的 -50%
        assert out["pct_chg"].iloc[1] == pytest.approx(0.0, abs=1e-6)
        # 前复权锚定最新交易日：最后一根价格保持原值
        assert out["close"].iloc[-1] == pytest.approx(5.90)
        # 除权前价格被折半，与除权后可比
        assert out["close"].iloc[0] == pytest.approx(5.75)

    def test_raises_when_adjustment_factors_missing(self) -> None:
        empty = pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

        with pytest.raises(Exception):
            TushareFetcher._apply_forward_adjustment(_raw_daily(), empty)

    def test_raises_when_factor_dates_do_not_intersect(self) -> None:
        factors = _adj_factors()
        factors["trade_date"] = ["20230102", "20230103"]

        with pytest.raises(Exception):
            TushareFetcher._apply_forward_adjustment(_raw_daily(), factors)

    def test_fetch_raw_data_uses_daily_plus_adj_factor_for_a_share(self) -> None:
        fetcher = TushareFetcher.__new__(TushareFetcher)
        fetcher._api = mock.MagicMock()
        fetcher._adj_factor_cache = {}
        fetcher._adj_factor_lock = __import__("threading").Lock()
        fetcher.rate_limit_per_minute = 80
        fetcher._call_count = 0
        fetcher._minute_start = None
        fetcher._check_rate_limit = lambda: None

        fetcher._api.daily.return_value = _raw_daily()
        fetcher._api.query.return_value = _adj_factors()

        out = fetcher._fetch_raw_data("600519", "2024-01-02", "2024-01-03")

        assert out["close"].tolist() == pytest.approx([5.75, 10.5])
        fetcher._api.daily.assert_called_once()
        assert fetcher._api.query.call_args[0][0] == "adj_factor"

    def test_hk_daily_uses_adjusted_endpoint(self) -> None:
        fetcher = TushareFetcher.__new__(TushareFetcher)
        fetcher._api = mock.MagicMock()
        fetcher._adj_factor_cache = {}
        fetcher._adj_factor_lock = __import__("threading").Lock()
        fetcher.rate_limit_per_minute = 80
        fetcher._call_count = 0
        fetcher._minute_start = None
        fetcher._check_rate_limit = lambda: None

        hk_frame = pd.DataFrame(
            {
                "ts_code": ["00700.HK"],
                "trade_date": ["20240103"],
                "open": [350.0], "high": [355.0], "low": [348.0], "close": [352.0],
                "vol": [1000.0], "amount": [352000.0],
            }
        )
        fetcher._api.hk_daily_adj.return_value = hk_frame

        out = fetcher._fetch_raw_data("00700.HK", "2024-01-02", "2024-01-03")

        assert out["close"].tolist() == [352.0]
        fetcher._api.hk_daily_adj.assert_called_once()
        # 关键：绝不静默回退到不复权的 hk_daily
        fetcher._api.hk_daily.assert_not_called()

    def test_hk_empty_adjusted_response_fails_instead_of_using_unadjusted(self) -> None:
        fetcher = TushareFetcher.__new__(TushareFetcher)
        fetcher._api = mock.MagicMock()
        fetcher._adj_factor_cache = {}
        fetcher._adj_factor_lock = __import__("threading").Lock()
        fetcher.rate_limit_per_minute = 80
        fetcher._call_count = 0
        fetcher._minute_start = None
        fetcher._check_rate_limit = lambda: None

        fetcher._api.hk_daily_adj.return_value = pd.DataFrame()

        with pytest.raises(Exception):
            fetcher._fetch_raw_data("00700.HK", "2024-01-02", "2024-01-03")

    def test_fetcher_declares_qfq(self) -> None:
        assert TushareFetcher.price_adjustment == "qfq"


# ---------------------------------------------------------------- 口径登记表


class TestPriceAdjustmentRegistry:
    def test_register_and_resolve(self) -> None:
        register_fetcher_price_adjustment("FakeQfqFetcher", "qfq")
        register_fetcher_price_adjustment("FakeRawFetcher", "none")

        assert price_adjustment_for_source("FakeQfqFetcher") == "qfq"
        assert price_adjustment_for_source("FakeRawFetcher") == "none"

    def test_unknown_source_is_none_not_raw(self) -> None:
        # 未登记 = 未知，不能等同于“不复权”，否则会把未知静默写成 none
        assert price_adjustment_for_source("NeverRegisteredFetcher") is None
        assert price_adjustment_for_source("") is None
        assert price_adjustment_for_source(None) is None

    def test_unknown_mode_is_not_registered(self) -> None:
        register_fetcher_price_adjustment("BogusFetcher", "half_adjusted")

        assert price_adjustment_for_source("BogusFetcher") is None
        assert "half_adjusted" not in VALID_PRICE_ADJUSTMENTS

    def test_manager_registers_fetcher_modes_on_index_refresh(self) -> None:
        from data_provider.base import DataFetcherManager

        class _StubFetcher:
            def __init__(self, name: str, mode: str) -> None:
                self.name = name
                self.price_adjustment = mode

            def _fetch_raw_data(self, *args, **kwargs):  # pragma: no cover - unused
                return pd.DataFrame()

            def _normalize_data(self, *args, **kwargs):  # pragma: no cover - unused
                return pd.DataFrame()

        manager = DataFetcherManager.__new__(DataFetcherManager)
        manager._fetchers = [_StubFetcher("StubQfq", "qfq"), _StubFetcher("StubRaw", "none")]
        manager._fetchers_by_name = {}
        manager._fetchers_lock = __import__("threading").RLock()
        manager._ensure_concurrency_guards = lambda: None

        manager._refresh_fetcher_indexes_locked()

        assert price_adjustment_for_source("StubQfq") == "qfq"
        assert price_adjustment_for_source("StubRaw") == "none"


# ---------------------------------------------------------------- 落库与检测


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03"],
            "open": [1.0, 1.1], "high": [1.2, 1.2], "low": [0.9, 1.0],
            "close": [1.1, 1.15], "volume": [100.0, 120.0],
            "amount": [110.0, 130.0], "pct_chg": [1.0, 1.5],
        }
    )


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


class TestStoragePriceAdjustment:
    def test_column_is_created_for_fresh_database(self, db) -> None:
        assert "price_adjustment" in db.to_dict() if hasattr(db, "to_dict") else True
        db.save_daily_data(_daily_frame(), "600519", data_source="EfinanceFetcher")

        with db.get_session() as session:
            from src.storage import StockDaily

            rows = session.query(StockDaily).all()
        assert rows and all(row.price_adjustment == "qfq" for row in rows)

    def test_unregistered_source_leaves_value_unknown(self, db) -> None:
        db.save_daily_data(_daily_frame(), "600519", data_source="SomeTestSource")

        with db.get_session() as session:
            from src.storage import StockDaily

            rows = session.query(StockDaily).all()
        assert rows and all(row.price_adjustment is None for row in rows)

    def test_explicit_value_wins_over_registry(self, db) -> None:
        db.save_daily_data(
            _daily_frame(), "600519", data_source="EfinanceFetcher", price_adjustment="none",
        )

        with db.get_session() as session:
            from src.storage import StockDaily

            rows = session.query(StockDaily).all()
        assert rows and all(row.price_adjustment == "none" for row in rows)

    def test_unknown_rewrite_does_not_null_out_known_value(self, db) -> None:
        db.save_daily_data(_daily_frame(), "600519", data_source="EfinanceFetcher")
        db.save_daily_data(_daily_frame(), "600519", data_source="SomeTestSource")

        with db.get_session() as session:
            from src.storage import StockDaily

            rows = session.query(StockDaily).all()
        assert rows and all(row.price_adjustment == "qfq" for row in rows)

    def test_summary_detects_mixed_modes(self, db) -> None:
        # 前一天来自前复权源，后一天来自不复权源
        db.save_daily_data(
            _daily_frame().iloc[[0]], "600519", data_source="EfinanceFetcher",
        )
        db.save_daily_data(
            _daily_frame().iloc[[1]], "600519", data_source="PytdxFetcher",
        )

        summary = db.get_price_adjustment_summary("600519")

        assert summary["mixed"] is True
        assert summary["declared"] == ["none", "qfq"]
        assert summary["unknown_rows"] == 0

    def test_summary_single_mode_is_not_mixed(self, db) -> None:
        db.save_daily_data(_daily_frame(), "600519", data_source="EfinanceFetcher")

        summary = db.get_price_adjustment_summary("600519")

        assert summary["mixed"] is False
        assert summary["modes"] == {"qfq": 2}

    def test_summary_ignores_unknown_rows(self, db) -> None:
        # 全新标的 + 未登记来源 = 插入 NULL（未知），未知行不构成混用证据
        db.save_daily_data(_daily_frame(), "000001", data_source="LegacyUnknownSource")

        summary = db.get_price_adjustment_summary("000001")

        assert summary["mixed"] is False
        assert summary["declared"] == []
        assert summary["unknown_rows"] == 2