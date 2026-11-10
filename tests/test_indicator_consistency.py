# -*- coding: utf-8 -*-
"""指标口径一致性与策略可求值性测试。

1. 量能基准不含当日（与 stock_analyzer / screening 一致）
2. MA 金叉/死叉可被求值（ma_golden_cross 策略不再是空转描述）
"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from src.agent.tools.analysis_tools import _detect_ma_cross, _handle_get_volume_analysis


def _volume_frame(volumes, closes=None) -> pd.DataFrame:
    n = len(volumes)
    dates = pd.date_range("2024-01-02", periods=n, freq="D")
    closes = closes if closes is not None else [10.0] * n
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
            "amount": [v * 10 for v in volumes],
        }
    )


class TestVolumeBaselineConsistency:
    def test_volume_ratio_excludes_current_day(self) -> None:
        # 前 5 天量能平稳 1000，今日放量到 3000（3 倍）
        volumes = [1000, 1000, 1000, 1000, 1000, 3000]
        closes = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]

        with mock.patch(
            "src.services.history_loader.load_history_df",
            return_value=(_volume_frame(volumes, closes), "TestFetcher"),
        ):
            out = _handle_get_volume_analysis("600519", days=6)

        # 若把当日计入分母：3000 / ((1000*5+3000)/6) = 2.57（低估）
        # 正确口径（不含当日）：3000 / 1000 = 3.0
        assert out["volume_ratio_vs_5d"] == pytest.approx(3.0)
        assert out["avg_volume_5d"] == pytest.approx(1000.0)

    def test_low_volume_day_ratio_is_not_diluted(self) -> None:
        volumes = [2000, 2000, 2000, 2000, 2000, 500]

        with mock.patch(
            "src.services.history_loader.load_history_df",
            return_value=(_volume_frame(volumes), "TestFetcher"),
        ):
            out = _handle_get_volume_analysis("600519", days=6)

        # 正确口径：500 / 2000 = 0.25；含当日则会被算成约 0.83（看起来"正常"）
        assert out["volume_ratio_vs_5d"] == pytest.approx(0.25)


class TestMaCrossEvaluation:
    def test_golden_cross_detected_within_lookback(self) -> None:
        # 9 根下跌 + 3 根强反弹：MA5 在最后一根窗口内上穿 MA10
        closes = pd.Series(
            [10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 8.8, 8.6, 8.4, 10.5, 11.5, 12.0]
        )

        out = _detect_ma_cross(closes, [5, 10], lookback=3)

        assert out["pairs"], "expected at least one MA pair to be evaluated"
        pair = out["pairs"]["ma5_ma10"]
        assert pair["golden_cross"] is True
        assert pair["death_cross"] is False
        assert pair["state"] == "above"
        assert pair["bars_since_cross"] is not None
        assert "ma5_ma10" in out["golden_cross_pairs"]

    def test_death_cross_detected(self) -> None:
        # 6 根上涨后 3 根急跌：MA5 下穿 MA10
        closes = pd.Series(
            [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 12.0, 11.0, 10.0, 9.5, 9.0, 8.5]
        )

        out = _detect_ma_cross(closes, [5, 10], lookback=3)

        pair = out["pairs"]["ma5_ma10"]
        assert pair["death_cross"] is True
        assert pair["state"] == "below"
        assert "ma5_ma10" in out["dead_cross_pairs"]

    def test_no_cross_reported_when_trend_is_steady(self) -> None:
        closes = pd.Series([float(10 + i) for i in range(15)])

        out = _detect_ma_cross(closes, [5, 10], lookback=3)

        pair = out["pairs"]["ma5_ma10"]
        assert pair["golden_cross"] is False
        assert pair["death_cross"] is False
        assert pair["state"] == "above"  # 多头排列延续，不等于刚发生金叉

    def test_insufficient_data_returns_empty_pairs(self) -> None:
        out = _detect_ma_cross(pd.Series([10.0, 10.1]), [5, 10], lookback=3)

        assert out["pairs"] == {}
        assert out["golden_cross_pairs"] == []