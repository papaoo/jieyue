# -*- coding: utf-8 -*-
"""Unit tests for backtest engine."""

import math
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from src.core.backtest_engine import BacktestEngine, EvaluationConfig


@dataclass
class Bar:
    date: date
    high: float
    low: float
    close: float
    open: Optional[float] = None


class BacktestEngineTestCase(unittest.TestCase):
    def _bars(self, start: date, closes, highs=None, lows=None, opens=None):
        highs = highs or closes
        lows = lows or closes
        opens = opens or closes
        bars = []
        for i, c in enumerate(closes):
            bars.append(
                Bar(
                    date=start + timedelta(days=i + 1),
                    high=highs[i],
                    low=lows[i],
                    close=c,
                    open=opens[i],
                )
            )
        return bars

    def test_buy_win_when_up(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [102, 104, 105], highs=[103, 105, 106], lows=[101, 103, 104])
        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=95,
            take_profit=110,
            config=cfg,
        )
        self.assertEqual(res["eval_status"], "completed")
        self.assertEqual(res["outcome"], "win")
        self.assertTrue(res["direction_correct"])  # up

    def test_sell_win_when_down_cash(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [98, 97, 96], highs=[99, 98, 97], lows=[97, 96, 95])
        res = BacktestEngine.evaluate_single(
            operation_advice="卖出",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=95,
            take_profit=110,
            config=cfg,
        )
        self.assertEqual(res["position_recommendation"], "cash")
        self.assertEqual(res["outcome"], "win")
        # 空仓建议按「同日开盘买入持有」的相对收益评估（v1 恒为 0，等于放弃评估卖出建议）：
        # 信号次日开盘 98 -> 窗口末收盘 96，持有亏 2.0408%，空仓相对收益 +2.0408%。
        self.assertAlmostEqual(res["simulated_return_pct"], 2.0408, places=4)
        self.assertEqual(res["simulated_return_basis"], "cash_vs_hold")
        self.assertEqual(res["first_hit"], "not_applicable")

    def test_wait_maps_to_cash_and_flat_direction(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        # Stock drops ~5%: AI said wait (neutral), stock moved significantly → loss
        bars = self._bars(date(2024, 1, 1), [98, 96, 95], highs=[99, 97, 96], lows=[97, 95, 94])
        res = BacktestEngine.evaluate_single(
            operation_advice="观望",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=95,
            take_profit=110,
            config=cfg,
        )
        self.assertEqual(res["position_recommendation"], "cash")
        self.assertEqual(res["direction_expected"], "flat")
        self.assertEqual(res["outcome"], "loss")

    def test_bearish_like_phrases_match_keyword_substring(self):
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("建议买入"),
            "long",
        )
        self.assertEqual(
            BacktestEngine.infer_direction_expected("继续持有"),
            "not_down",
        )
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("建议持有"),
            "long",
        )
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("建议洗盘观察"),
            "long",
        )

    def test_range_bound_watch_is_treated_as_hold_long_path(self):
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("震荡观望"),
            "long",
        )
        self.assertEqual(
            BacktestEngine.infer_direction_expected("Range-bound watch"),
            "not_down",
        )
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("Range-bound watch"),
            "long",
        )

    def test_shakeout_watch_is_treated_as_hold_long_path(self):
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("洗盘观察"),
            "long",
        )
        self.assertEqual(
            BacktestEngine.infer_direction_expected("Shakeout watch"),
            "not_down",
        )
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("Hold and watch"),
            "long",
        )

    def test_hold_win_when_flat(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [100.5, 100.2, 101], highs=[101, 101, 101], lows=[99.8, 99.9, 100])
        res = BacktestEngine.evaluate_single(
            operation_advice="持有",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["outcome"], "win")

    def test_hold_win_when_up(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [102, 103, 104], highs=[103, 104, 105], lows=[101, 102, 103])
        res = BacktestEngine.evaluate_single(
            operation_advice="持有",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["outcome"], "win")

    def test_decision_signal_helper_classifies_structured_up_not_down_and_not_up(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0, engine_version="decision-signal-v1")

        up = BacktestEngine.evaluate_decision_signal(
            direction_expected="up",
            anchor_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=self._bars(date(2024, 1, 1), [101, 102, 103]),
            config=cfg,
        )
        not_down = BacktestEngine.evaluate_decision_signal(
            direction_expected="not_down",
            anchor_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=self._bars(date(2024, 1, 1), [99.5, 99, 99]),
            config=cfg,
        )
        not_up = BacktestEngine.evaluate_decision_signal(
            direction_expected="not_up",
            anchor_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=self._bars(date(2024, 1, 1), [100.5, 101, 101.5]),
            config=cfg,
        )
        not_up_miss = BacktestEngine.evaluate_decision_signal(
            direction_expected="not_up",
            anchor_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=self._bars(date(2024, 1, 1), [101, 102, 103]),
            config=cfg,
        )

        self.assertEqual(up["outcome"], "hit")
        self.assertEqual(not_down["outcome"], "neutral")
        self.assertEqual(not_up["outcome"], "hit")
        self.assertEqual(not_up_miss["outcome"], "miss")
        # 决策点度量：方向命中以「决策点收盘价」为基准（预测从决策点做出，
        # 衡量预测质量用决策点不含前视偏差；前视只存在于按当日收盘价成交）
        self.assertAlmostEqual(up["stock_return_pct"], 3.0, places=4)

    def test_decision_signal_helper_rejects_non_finite_prices(self):
        cfg = EvaluationConfig(eval_window_days=1, neutral_band_pct=2.0, engine_version="decision-signal-v1")

        bad_start = BacktestEngine.evaluate_decision_signal(
            direction_expected="up",
            anchor_date=date(2024, 1, 1),
            start_price=math.nan,
            forward_bars=self._bars(date(2024, 1, 1), [103]),
            config=cfg,
        )
        bad_end = BacktestEngine.evaluate_decision_signal(
            direction_expected="up",
            anchor_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=[Bar(date=date(2024, 1, 2), high=101, low=99, close=math.nan)],
            config=cfg,
        )
        bad_bounds = BacktestEngine.evaluate_decision_signal(
            direction_expected="up",
            anchor_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=[Bar(date=date(2024, 1, 2), high=math.inf, low=-math.inf, close=103)],
            config=cfg,
        )

        self.assertEqual(bad_start["eval_status"], "unable")
        self.assertEqual(bad_start["unable_reason"], "invalid_anchor_price")
        self.assertEqual(bad_end["eval_status"], "unable")
        self.assertEqual(bad_end["unable_reason"], "invalid_end_close")
        self.assertEqual(bad_bounds["eval_status"], "completed")
        # 决策点度量仍以锚点收盘价为基准
        self.assertEqual(bad_bounds["stock_return_pct"], 3.0)
        self.assertIsNone(bad_bounds["max_high"])
        self.assertIsNone(bad_bounds["min_low"])

    def test_decision_signal_helper_does_not_change_evaluate_single_hold_behavior(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        res = BacktestEngine.evaluate_single(
            operation_advice="持有",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=self._bars(date(2024, 1, 1), [100.2, 100.4, 100.6]),
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )

        self.assertEqual(res["direction_expected"], "not_down")
        self.assertEqual(res["outcome"], "win")

    def test_stop_loss_hit_first(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [99, 98, 97], highs=[101, 100, 99], lows=[94, 97, 96])
        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=95,
            take_profit=110,
            config=cfg,
        )
        self.assertTrue(res["hit_stop_loss"])
        self.assertEqual(res["first_hit"], "stop_loss")
        self.assertEqual(res["simulated_exit_reason"], "stop_loss")

    def test_take_profit_hit_first(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [105, 106, 107], highs=[111, 107, 108], lows=[103, 105, 106])
        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=95,
            take_profit=110,
            config=cfg,
        )
        self.assertTrue(res["hit_take_profit"])
        self.assertEqual(res["first_hit"], "take_profit")
        self.assertEqual(res["simulated_exit_reason"], "take_profit")

    def test_ambiguous_same_day(self):
        cfg = EvaluationConfig(eval_window_days=2, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [100, 100], highs=[111, 100], lows=[94, 99])
        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=95,
            take_profit=110,
            config=cfg,
        )
        self.assertEqual(res["first_hit"], "ambiguous")
        self.assertEqual(res["simulated_exit_reason"], "ambiguous_stop_loss")

    def test_buy_loss_when_down(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [98, 96, 95], highs=[99, 97, 96], lows=[97, 95, 94])
        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=93,
            take_profit=110,
            config=cfg,
        )
        self.assertEqual(res["eval_status"], "completed")
        self.assertEqual(res["outcome"], "loss")
        self.assertFalse(res["direction_correct"])

    def test_hold_loss_when_down(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [98, 96, 95], highs=[99, 97, 96], lows=[97, 95, 94])
        res = BacktestEngine.evaluate_single(
            operation_advice="持有",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["direction_expected"], "not_down")
        self.assertEqual(res["outcome"], "loss")
        self.assertFalse(res["direction_correct"])

    def test_sell_loss_when_up(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [102, 104, 106], highs=[103, 105, 107], lows=[101, 103, 105])
        res = BacktestEngine.evaluate_single(
            operation_advice="卖出",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["position_recommendation"], "cash")
        self.assertEqual(res["direction_expected"], "down")
        self.assertEqual(res["outcome"], "loss")
        self.assertFalse(res["direction_correct"])

    def test_neutral_outcome(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [100.5, 100.2, 100.8], highs=[101, 101, 101], lows=[100, 100, 100])
        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["direction_expected"], "up")
        self.assertEqual(res["outcome"], "neutral")
        self.assertIsNone(res["direction_correct"])

    def test_direction_correct_false_buy_down(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [97, 95, 94], highs=[98, 96, 95], lows=[96, 94, 93])
        res = BacktestEngine.evaluate_single(
            operation_advice="buy",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["direction_expected"], "up")
        self.assertEqual(res["outcome"], "loss")
        self.assertFalse(res["direction_correct"])

    def test_insufficient_data(self):
        cfg = EvaluationConfig(eval_window_days=5, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [100, 101])
        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["eval_status"], "insufficient_data")

    def test_unrecognized_advice_defaults_to_cash(self):
        cfg = EvaluationConfig(eval_window_days=3, neutral_band_pct=2.0)
        bars = self._bars(date(2024, 1, 1), [102, 104, 105], highs=[103, 105, 106], lows=[101, 103, 104])
        res = BacktestEngine.evaluate_single(
            operation_advice="some gibberish text",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=cfg,
        )
        self.assertEqual(res["position_recommendation"], "cash")
        self.assertEqual(res["direction_expected"], "flat")

    def test_none_empty_advice_defaults_to_cash(self):
        for advice in [None, "", "   "]:
            pos = BacktestEngine.infer_position_recommendation(advice)
            direction = BacktestEngine.infer_direction_expected(advice)
            self.assertEqual(pos, "cash", f"Expected cash for advice={advice!r}")
            self.assertEqual(direction, "flat", f"Expected flat for advice={advice!r}")

    def test_negated_sell_not_classified_bearish(self):
        # "do not sell" negates "sell" — should NOT be direction=down
        self.assertNotEqual(BacktestEngine.infer_direction_expected("do not sell"), "down")

    def test_chinese_negated_sell_not_bearish(self):
        # "不要卖出" = "don't sell" — should NOT be direction=down
        self.assertNotEqual(BacktestEngine.infer_direction_expected("不要卖出"), "down")

    def test_conditional_support_phrase_not_negating_hold(self):
        # "不跌破支撑继续持有" means conditional support hold, not explicit negation of hold.
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("不跌破支撑继续持有"),
            "long",
        )
        self.assertEqual(
            BacktestEngine.infer_direction_expected("不跌破支撑继续持有"),
            "not_down",
        )

    def test_wait_then_buy_classified_as_cash(self):
        # "wait" matches first in priority order → cash
        pos = BacktestEngine.infer_position_recommendation("wait for a dip then buy")
        self.assertEqual(pos, "cash")

    def test_wait_phrase_before_bullish_phrases_stays_wait(self):
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("先观望再买入"),
            "cash",
        )
        self.assertEqual(
            BacktestEngine.infer_direction_expected("先观望再买入"),
            "flat",
        )
        self.assertEqual(
            BacktestEngine.infer_position_recommendation("观望后买入"),
            "cash",
        )
        self.assertEqual(
            BacktestEngine.infer_direction_expected("观望后买入"),
            "flat",
        )


class BacktestEngineExecutionSemanticsTestCase(unittest.TestCase):
    """v2 执行语义：次日入场、成本、跳空保护、空仓相对收益。"""

    def _cfg(self, **overrides):
        params = {"eval_window_days": 3, "neutral_band_pct": 2.0}
        params.update(overrides)
        return EvaluationConfig(**params)

    def _bars(self, start, closes, opens=None, highs=None, lows=None):
        opens = opens or closes
        highs = highs or closes
        lows = lows or closes
        return [
            Bar(
                date=start + timedelta(days=i + 1),
                open=opens[i],
                high=highs[i],
                low=lows[i],
                close=closes[i],
            )
            for i in range(len(closes))
        ]

    def test_entry_uses_next_open_not_signal_close(self):
        # 信号当日收盘 100（start_price），次日开盘跳空到 105
        bars = self._bars(date(2024, 1, 1), [106, 107, 108], opens=[105, 107, 108])

        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=self._cfg(),
        )

        self.assertEqual(res["entry_price_basis"], "next_open")
        self.assertEqual(res["entry_price"], 105)
        # 决策点度量：预测从决策点做出，用 T 收盘 100 -> +8%
        self.assertAlmostEqual(res["stock_return_pct"], 8.0, places=4)
        # 交易损益基准：次日开盘 105 -> +2.857%（v1 会用 100 当成交价，虚增收益）
        self.assertAlmostEqual(res["trade_return_pct"], (108 - 105) / 105 * 100, places=4)

    def test_zero_lag_opt_in_returns_to_signal_close(self):
        bars = self._bars(date(2024, 1, 1), [101, 102, 103])

        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=self._cfg(execution_lag_days=0),
        )

        self.assertEqual(res["entry_price_basis"], "signal_close")
        self.assertEqual(res["entry_price"], 100)

    def test_long_return_is_net_of_round_trip_costs(self):
        bars = self._bars(date(2024, 1, 1), [100, 110, 110])

        with_costs = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=self._cfg(),
        )
        free = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=self._cfg(commission_bps=0.0, stamp_duty_bps=0.0, slippage_bps=0.0),
        )

        # 无成本时：入场 100（次日开盘）-> 窗口末 110 = +10%
        self.assertAlmostEqual(free["simulated_return_pct"], 10.0, places=4)
        # 默认成本（买入 8bp + 卖出 13bp）必然拉低收益
        self.assertLess(with_costs["simulated_return_pct"], free["simulated_return_pct"])
        self.assertEqual(with_costs["cost_bps_per_round_trip"], 21.0)

    def test_stop_loss_gap_down_fills_at_open_not_stop_price(self):
        # 次日开盘 90（已跌破止损 95），日内最低 88
        bars = self._bars(date(2024, 1, 1), [90, 89, 88], opens=[90, 89, 88], lows=[88, 87, 86])

        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=95,
            take_profit=None,
            config=self._cfg(),
        )

        self.assertEqual(res["first_hit"], "stop_loss")
        # 跳空保护：成交价是开盘 90，而不是理想的止损价 95
        self.assertEqual(res["simulated_exit_price"], 90)

    def test_take_profit_gap_up_fills_at_open(self):
        bars = self._bars(date(2024, 1, 1), [120, 121, 122], opens=[120, 121, 122], highs=[122, 123, 124])

        res = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=110,
            config=self._cfg(),
        )

        self.assertEqual(res["first_hit"], "take_profit")
        self.assertEqual(res["simulated_exit_price"], 120)

    def test_cash_advice_scores_relative_to_holding(self):
        # 信号次日开盘 95 -> 窗口末收盘 90：持有亏 5.2632%，空仓相对收益 +5.2632%
        bars = self._bars(date(2024, 1, 1), [95, 92, 90])

        res = BacktestEngine.evaluate_single(
            operation_advice="卖出",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=self._cfg(),
        )

        self.assertEqual(res["position_recommendation"], "cash")
        self.assertAlmostEqual(res["simulated_return_pct"], 5.2632, places=4)
        self.assertIsNone(res["simulated_entry_price"])

    def test_cash_advice_penalizes_missing_a_rally(self):
        bars = self._bars(date(2024, 1, 1), [105, 108, 110])

        res = BacktestEngine.evaluate_single(
            operation_advice="卖出",
            analysis_date=date(2024, 1, 1),
            start_price=100,
            forward_bars=bars,
            stop_loss=None,
            take_profit=None,
            config=self._cfg(),
        )

        # 空仓踏空 = 负的相对收益（v1 恒为 0，无法评估卖出建议）
        self.assertAlmostEqual(res["simulated_return_pct"], -4.7619, places=4)

    def test_summary_reports_profit_factor_drawdown_and_benchmark(self):
        @dataclass
        class Row:
            eval_status: str = "completed"
            position_recommendation: str = "long"
            outcome: str = "win"
            direction_correct: bool = True
            stock_return_pct: Optional[float] = None
            simulated_return_pct: Optional[float] = None
            hit_stop_loss: Optional[bool] = None
            hit_take_profit: Optional[bool] = None
            first_hit: Optional[str] = None
            first_hit_trading_days: Optional[int] = None
            operation_advice: Optional[str] = "买入"
            analysis_date: Optional[date] = None

        rows = [
            Row(outcome="win", simulated_return_pct=6.0, stock_return_pct=5.0,
                analysis_date=date(2024, 1, 2)),
            Row(outcome="win", simulated_return_pct=4.0, stock_return_pct=3.0,
                analysis_date=date(2024, 1, 3)),
            Row(outcome="loss", simulated_return_pct=-2.0, stock_return_pct=-3.0,
                analysis_date=date(2024, 1, 4)),
            Row(outcome="loss", simulated_return_pct=-4.0, stock_return_pct=-3.0,
                analysis_date=date(2024, 1, 5)),
        ]

        summary = BacktestEngine.compute_summary(
            results=rows,
            scope="overall",
            code=None,
            eval_window_days=3,
            engine_version="v2",
        )

        # 盈亏比 = 平均盈利 / 平均亏损绝对值 = 5 / 3
        self.assertAlmostEqual(summary["profit_factor"], 5 / 3, places=4)
        self.assertEqual(summary["avg_win_return_pct"], 5.0)
        self.assertEqual(summary["avg_loss_return_pct"], -3.0)
        self.assertEqual(summary["largest_win_pct"], 6.0)
        self.assertEqual(summary["largest_loss_pct"], -4.0)
        self.assertEqual(summary["benchmark_avg_return_pct"], 0.5)
        # avg_simulated 1.0% - avg_stock 0.5% = 超额 0.5%
        self.assertAlmostEqual(summary["excess_vs_benchmark_pct"], 0.5, places=4)
        self.assertLess(summary["max_drawdown_pct"], 0)
        self.assertEqual(summary["drawdown_series_points"], 4)


if __name__ == "__main__":
    unittest.main()
