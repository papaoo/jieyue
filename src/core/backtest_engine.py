# -*- coding: utf-8 -*-
"""Backtesting evaluation engine (pure logic).

This module is intentionally DB-agnostic: it operates on plain values or
objects that look like daily OHLC bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence


OVERALL_SENTINEL_CODE = "__overall__"


class DailyBarLike(Protocol):
    """Protocol for objects representing a daily OHLC bar."""

    date: date
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]


class BacktestResultLike(Protocol):
    """Protocol for objects that behave like a stored BacktestResult."""

    eval_status: str
    position_recommendation: Optional[str]
    outcome: Optional[str]
    direction_correct: Optional[bool]
    stock_return_pct: Optional[float]
    simulated_return_pct: Optional[float]
    hit_stop_loss: Optional[bool]
    hit_take_profit: Optional[bool]
    first_hit: Optional[str]
    first_hit_trading_days: Optional[int]
    operation_advice: Optional[str]


@dataclass(frozen=True)
class EvaluationConfig:
    """评估参数。

    v2 起修正了 v1 的两个系统性偏差：
    1. **前视偏差**：v1 用信号当日收盘价入场（信号本身产生于当日收盘），
       等于用收盘后的信息按收盘价成交。v2 默认 ``execution_lag_days=1``，
       即信号次日执行（优先次日开盘价，无 open 时退化为次日收盘价）。
    2. **零成本**：v1 的模拟收益是裸价差，没有手续费、印花税和滑点，
       系统性高估策略表现。v2 默认按 A 股口径计入双边成本。
    """

    eval_window_days: int
    neutral_band_pct: float = 2.0
    engine_version: str = "v2"
    # 执行语义
    execution_lag_days: int = 1
    # 成本模型（单边，bps = 万分之一）
    commission_bps: float = 3.0   # 佣金
    stamp_duty_bps: float = 5.0   # 印花税（仅卖出收取，A 股单向）
    slippage_bps: float = 5.0     # 滑点

    @property
    def entry_cost_rate(self) -> float:
        """买入单边费率（佣金 + 滑点）。"""
        return max(0.0, (float(self.commission_bps) + float(self.slippage_bps)) / 10000.0)

    @property
    def exit_cost_rate(self) -> float:
        """卖出单边费率（佣金 + 印花税 + 滑点）。"""
        return max(
            0.0,
            (float(self.commission_bps) + float(self.stamp_duty_bps) + float(self.slippage_bps)) / 10000.0,
        )


class BacktestEngine:
    """LLM 研判建议评估引擎（单标的、日线、long/cash 两态）。

    .. warning::
       **它不是组合回测引擎。** 本引擎评估的是「历史研判建议的事后表现」：
       输入一份 LLM ``operation_advice``（或结构化 action），输出该建议在
       后续 N 个交易日的方向命中与模拟收益。它没有组合资金、仓位分配、
       多标的权重和撮合队列，不能直接当作策略净值曲线使用。
       真实组合回测请使用 :mod:`src.core.etf_rotation`（次日执行 + 成本 +
       停牌桥接 + 净值/夏普/最大回撤）。
    """

    # Operation advice keywords (Chinese + English)
    _BULLISH_KEYWORDS = (
        "买入",
        "加仓",
        "强烈买入",
        "增持",
        "建仓",
        "strong buy",
        "buy",
        "add",
    )
    _BEARISH_KEYWORDS = (
        "卖出",
        "减仓",
        "强烈卖出",
        "清仓",
        "strong sell",
        "sell",
        "reduce",
    )
    _HOLD_KEYWORDS = (
        "持有",
        "震荡观望",
        "洗盘观察",
        "持有观察",
        "hold",
        "range-bound watch",
        "shakeout watch",
        "hold and watch",
    )
    _WAIT_KEYWORDS = (
        "观望",
        "等待",
        "wait",
    )

    # Negation prefixes (trailing spaces stripped for suffix-matching against prefix text).
    # English patterns include trailing space in their canonical form; rstrip is
    # applied during matching so "do not" matches prefix "do not " or "do not".
    _NEGATION_PATTERNS = (
        "not", "don't", "do not", "no", "never", "avoid",  # English
        "不要", "不", "别", "勿", "没有",  # Chinese
    )

    _NEGATION_CONNECTOR_WORDS = (
        "建议",
        "应",
        "应当",
        "宜",
        "先",
        "再",
        "暂",
        "不必",
        "必须",
        "无需",
    )

    @classmethod
    def infer_direction_expected(cls, operation_advice: Optional[str]) -> str:
        """Infer expected direction: up/down/not_down/flat."""
        text = cls._normalize_text(operation_advice)
        if cls._matches_intent(text, cls._BEARISH_KEYWORDS):
            return "down"
        if cls._first_intent_position(text, cls._WAIT_KEYWORDS) is not None:
            wait_pos = cls._first_intent_position(text, cls._WAIT_KEYWORDS)
            bullish_pos = cls._first_intent_position(text, cls._BULLISH_KEYWORDS)
            hold_pos = cls._first_intent_position(text, cls._HOLD_KEYWORDS)
            if (bullish_pos is None or wait_pos < bullish_pos) and (
                hold_pos is None or wait_pos < hold_pos
            ):
                return "flat"
        if cls._matches_intent(text, cls._BULLISH_KEYWORDS):
            return "up"
        if cls._matches_intent(text, cls._HOLD_KEYWORDS):
            return "not_down"
        if cls._matches_intent(text, cls._WAIT_KEYWORDS):
            return "flat"
        return "flat"

    @classmethod
    def infer_position_recommendation(cls, operation_advice: Optional[str]) -> str:
        """Infer recommended position: long/cash (long-only system).

        Priority: bearish/wait -> cash, bullish/hold -> long, unrecognized -> cash.
        """
        text = cls._normalize_text(operation_advice)
        if cls._matches_intent(text, cls._BEARISH_KEYWORDS):
            return "cash"
        wait_pos = cls._first_intent_position(text, cls._WAIT_KEYWORDS)
        if wait_pos is not None:
            bullish_pos = cls._first_intent_position(text, cls._BULLISH_KEYWORDS)
            hold_pos = cls._first_intent_position(text, cls._HOLD_KEYWORDS)
            if (bullish_pos is None or wait_pos < bullish_pos) and (
                hold_pos is None or wait_pos < hold_pos
            ):
                return "cash"
        if cls._matches_intent(text, cls._BULLISH_KEYWORDS) or cls._matches_intent(text, cls._HOLD_KEYWORDS):
            return "long"
        if cls._matches_intent(text, cls._WAIT_KEYWORDS):
            return "cash"
        return "cash"

    @classmethod
    def _resolve_entry_price(
        cls,
        start_price: float,
        window_bars: Sequence[DailyBarLike],
        config: EvaluationConfig,
    ) -> tuple:
        """Resolve the executable entry price for a signal produced at T's close.

        The signal is generated *after* T's close, so filling at T's close is a
        look-ahead bias. With ``execution_lag_days=1`` we enter at the next
        session's open (the earliest price a follower could actually trade at);
        when a bar lacks ``open`` we degrade to that bar's close, which is still
        one session later and therefore free of look-ahead.

        Returns ``(entry_price, basis)``.
        """
        lag = max(0, int(getattr(config, "execution_lag_days", 1)))
        if lag <= 0:
            # Explicitly opted into same-close execution (research only).
            return start_price, "signal_close"

        index = lag - 1
        if index < len(window_bars):
            bar = window_bars[index]
            open_price = cls._finite_optional_float(getattr(bar, "open", None))
            if open_price is not None and open_price > 0:
                return open_price, "next_open"
            close_price = cls._finite_optional_float(getattr(bar, "close", None))
            if close_price is not None and close_price > 0:
                return close_price, "next_close"
        return start_price, "signal_close_fallback"

    @classmethod
    def _net_return_pct(
        cls,
        entry_price: float,
        exit_price: float,
        config: EvaluationConfig,
    ) -> Optional[float]:
        """Return after round-trip costs (buy entry + sell exit)."""
        if entry_price is None or exit_price is None or entry_price <= 0:
            return None
        buy = entry_price * (1.0 + config.entry_cost_rate)
        sell = exit_price * (1.0 - config.exit_cost_rate)
        if buy <= 0:
            return None
        return round((sell - buy) / buy * 100, 4)

    @classmethod
    def evaluate_single(
        cls,
        *,
        operation_advice: Optional[str],
        analysis_date: date,
        start_price: float,
        forward_bars: Sequence[DailyBarLike],
        stop_loss: Optional[float],
        take_profit: Optional[float],
        config: EvaluationConfig,
    ) -> Dict[str, Any]:
        """Evaluate one historical analysis against forward daily bars.

        Two bases are deliberately kept apart:

        - ``stock_return_pct`` — measured from the **decision point** (the signal
          bar's close). Direction hit / win-loss classification uses it, because
          the prediction is made at that point and evaluating a prediction from
          that point carries no look-ahead.
        - ``entry_price`` / ``trade_return_pct`` / ``simulated_return_pct`` —
          measured from the **executable** price (next session by default) and
          net of round-trip costs. Tradeable P&L must never fill at the signal
          bar's close, which is the actual look-ahead in the old behavior.

        Notes
        -----
        - Long positions pay round-trip costs; cash positions are measured as
          *relative* performance versus holding from the same entry (no trade
          happens, so no costs apply).
        - Daily bars cannot determine intraday ordering. If stop-loss and
          take-profit are both touched in the same bar, we record
          first_hit="ambiguous" and assume stop-loss first for simulated exit.
        """

        if start_price is None or start_price <= 0:
            return {
                "analysis_date": analysis_date,
                "operation_advice": operation_advice,
                "position_recommendation": cls.infer_position_recommendation(operation_advice),
                "direction_expected": cls.infer_direction_expected(operation_advice),
                "eval_status": "error",
            }

        eval_days = int(config.eval_window_days)
        if eval_days <= 0:
            raise ValueError("eval_window_days must be positive")

        if len(forward_bars) < eval_days:
            return {
                "analysis_date": analysis_date,
                "operation_advice": operation_advice,
                "position_recommendation": cls.infer_position_recommendation(operation_advice),
                "direction_expected": cls.infer_direction_expected(operation_advice),
                "eval_status": "insufficient_data",
                "eval_window_days": eval_days,
            }

        window_bars = list(forward_bars[:eval_days])
        end_close = window_bars[-1].close
        highs = [b.high for b in window_bars if b.high is not None]
        lows = [b.low for b in window_bars if b.low is not None]
        max_high = max(highs) if highs else None
        min_low = min(lows) if lows else None

        entry_price, entry_basis = cls._resolve_entry_price(start_price, window_bars, config)

        # 决策点度量（预测质量）：以信号当日收盘价为基准。
        # 交易损益（可成交）：以次日开盘价为基准并计入成本。
        # 两者必须分开——前视偏差只存在于"按信号当日收盘价成交"这个动作里，
        # 而预测本身是从决策点开始计量的。
        decision_return_pct: Optional[float]
        if end_close is None:
            decision_return_pct = None
        else:
            decision_return_pct = (end_close - start_price) / start_price * 100

        trade_return_pct: Optional[float]
        if end_close is None:
            trade_return_pct = None
        else:
            trade_return_pct = (end_close - entry_price) / entry_price * 100

        direction_expected = cls.infer_direction_expected(operation_advice)
        position = cls.infer_position_recommendation(operation_advice)

        outcome, direction_correct = cls._classify_outcome(
            stock_return_pct=decision_return_pct,
            direction_expected=direction_expected,
            neutral_band_pct=config.neutral_band_pct,
        )

        (
            hit_stop_loss,
            hit_take_profit,
            first_hit,
            first_hit_date,
            first_hit_days,
            simulated_exit_price,
            simulated_exit_reason,
        ) = cls._evaluate_targets(
            position=position,
            stop_loss=stop_loss,
            take_profit=take_profit,
            window_bars=window_bars,
            end_close=end_close,
        )

        if position != "long":
            # 空仓建议相对「同日开盘买入并持有」的收益：不发生交易，因此不计成本。
            # 正值 = 避跌有效；负值 = 踏空代价（v1 恒为 0，等于放弃评估卖出建议）。
            cash_relative_return_pct = (
                round(-trade_return_pct, 4) if trade_return_pct is not None else None
            )
            simulated_entry_price = None
            simulated_return_pct = cash_relative_return_pct
            simulated_return_basis = "cash_vs_hold"
        else:
            simulated_entry_price = entry_price
            simulated_return_pct = (
                cls._net_return_pct(entry_price, simulated_exit_price, config)
                if simulated_exit_price is not None
                else None
            )
            simulated_return_basis = "long_net_of_costs"

        return {
            "analysis_date": analysis_date,
            "eval_window_days": eval_days,
            "engine_version": config.engine_version,
            "eval_status": "completed",
            "operation_advice": operation_advice,
            "position_recommendation": position,
            "start_price": start_price,
            "entry_price": entry_price,
            "entry_price_basis": entry_basis,
            "end_close": end_close,
            "max_high": max_high,
            "min_low": min_low,
            "stock_return_pct": decision_return_pct,
            "trade_return_pct": trade_return_pct,
            "direction_expected": direction_expected,
            "direction_correct": direction_correct,
            "outcome": outcome,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "hit_stop_loss": hit_stop_loss,
            "hit_take_profit": hit_take_profit,
            "first_hit": first_hit,
            "first_hit_date": first_hit_date,
            "first_hit_trading_days": first_hit_days,
            "simulated_entry_price": simulated_entry_price,
            "simulated_exit_price": simulated_exit_price,
            "simulated_exit_reason": simulated_exit_reason,
            "simulated_return_pct": simulated_return_pct,
            "simulated_return_basis": simulated_return_basis,
            "cost_bps_per_round_trip": round(
                (config.entry_cost_rate + config.exit_cost_rate) * 10000, 2
            ),
        }

    @classmethod
    def evaluate_decision_signal(
        cls,
        *,
        direction_expected: str,
        anchor_date: date,
        start_price: float,
        forward_bars: Sequence[DailyBarLike],
        config: EvaluationConfig,
    ) -> Dict[str, Any]:
        """Evaluate a structured DecisionSignal action without text inference."""

        start_price_value = cls._finite_optional_float(start_price)
        if start_price_value is None or start_price_value <= 0:
            return {
                "anchor_date": anchor_date,
                "direction_expected": direction_expected,
                "eval_status": "unable",
                "unable_reason": "invalid_anchor_price",
            }

        eval_days = int(config.eval_window_days)
        if eval_days <= 0:
            raise ValueError("eval_window_days must be positive")

        if len(forward_bars) < eval_days:
            return {
                "anchor_date": anchor_date,
                "eval_window_days": eval_days,
                "engine_version": config.engine_version,
                "direction_expected": direction_expected,
                "eval_status": "unable",
                "unable_reason": "insufficient_forward_bars",
            }

        window_bars = list(forward_bars[:eval_days])
        raw_end_close = window_bars[-1].close
        end_close = cls._finite_optional_float(raw_end_close)
        highs: List[float] = []
        lows: List[float] = []
        for bar in window_bars:
            high = cls._finite_optional_float(bar.high)
            low = cls._finite_optional_float(bar.low)
            if high is not None:
                highs.append(high)
            if low is not None:
                lows.append(low)
        max_high = max(highs) if highs else None
        min_low = min(lows) if lows else None

        # 方向/命中度量以「决策点收盘价」为基准：预测是从这里做出的，
        # 衡量预测质量用决策点是标准做法，本身不含前视偏差。
        stock_return_pct: Optional[float]
        if end_close is None:
            stock_return_pct = None
        else:
            stock_return_pct = (end_close - start_price_value) / start_price_value * 100

        outcome, direction_correct = cls._classify_signal_outcome(
            stock_return_pct=stock_return_pct,
            direction_expected=direction_expected,
            neutral_band_pct=config.neutral_band_pct,
        )

        if stock_return_pct is None:
            return {
                "anchor_date": anchor_date,
                "eval_window_days": eval_days,
                "engine_version": config.engine_version,
                "direction_expected": direction_expected,
                "eval_status": "unable",
                "unable_reason": "missing_end_close" if raw_end_close is None else "invalid_end_close",
                "start_price": start_price_value,
                "end_close": end_close,
                "max_high": max_high,
                "min_low": min_low,
            }

        return {
            "anchor_date": anchor_date,
            "eval_window_days": eval_days,
            "engine_version": config.engine_version,
            "eval_status": "completed",
            "direction_expected": direction_expected,
            "direction_correct": direction_correct,
            "outcome": outcome,
            "start_price": start_price_value,
            "end_close": end_close,
            "max_high": max_high,
            "min_low": min_low,
            "stock_return_pct": stock_return_pct,
        }

    @classmethod
    def compute_summary(
        cls,
        *,
        results: Iterable[BacktestResultLike],
        scope: str,
        code: Optional[str],
        eval_window_days: int,
        engine_version: str,
    ) -> Dict[str, Any]:
        """Aggregate BacktestResult rows into summary metrics."""
        results_list = list(results)

        total = len(results_list)
        completed = [r for r in results_list if (r.eval_status or "") == "completed"]
        insufficient_count = sum(1 for r in results_list if (r.eval_status or "") == "insufficient_data")

        long_count = sum(1 for r in completed if (r.position_recommendation or "") == "long")
        cash_count = sum(1 for r in completed if (r.position_recommendation or "") == "cash")

        win_count = sum(1 for r in completed if (r.outcome or "") == "win")
        loss_count = sum(1 for r in completed if (r.outcome or "") == "loss")
        neutral_count = sum(1 for r in completed if (r.outcome or "") == "neutral")

        direction_denominator = sum(1 for r in completed if r.direction_correct is not None)
        direction_numerator = sum(1 for r in completed if r.direction_correct is True)
        direction_accuracy_pct = (
            round(direction_numerator / direction_denominator * 100, 2) if direction_denominator else None
        )

        win_loss_denominator = win_count + loss_count
        win_rate_pct = round(win_count / win_loss_denominator * 100, 2) if win_loss_denominator else None
        neutral_rate_pct = round(neutral_count / len(completed) * 100, 2) if completed else None

        avg_stock_return_pct = cls._average([r.stock_return_pct for r in completed])
        avg_simulated_return_pct = cls._average([r.simulated_return_pct for r in completed])

        stop_applicable = [
            r
            for r in completed
            if (r.position_recommendation or "") == "long" and r.hit_stop_loss is not None
        ]
        stop_loss_trigger_rate = (
            round(sum(1 for r in stop_applicable if r.hit_stop_loss is True) / len(stop_applicable) * 100, 2)
            if stop_applicable
            else None
        )

        take_profit_applicable = [
            r
            for r in completed
            if (r.position_recommendation or "") == "long" and r.hit_take_profit is not None
        ]
        take_profit_trigger_rate = (
            round(
                sum(1 for r in take_profit_applicable if r.hit_take_profit is True) / len(take_profit_applicable) * 100,
                2,
            )
            if take_profit_applicable
            else None
        )

        any_target_applicable = [
            r
            for r in completed
            if (r.position_recommendation or "") == "long"
            and (r.hit_stop_loss is not None or r.hit_take_profit is not None)
        ]
        ambiguous_rate = (
            round(
                sum(1 for r in any_target_applicable if (r.first_hit or "") == "ambiguous")
                / len(any_target_applicable)
                * 100,
                2,
            )
            if any_target_applicable
            else None
        )
        avg_days_to_first_hit = cls._average(
            [
                float(r.first_hit_trading_days)
                for r in any_target_applicable
                if r.first_hit_trading_days is not None and (r.first_hit or "") in ("stop_loss", "take_profit", "ambiguous")
            ]
        )

        advice_breakdown = cls._compute_advice_breakdown(completed)
        diagnostics = cls._compute_diagnostics(results_list)

        risk_metrics = cls._compute_risk_metrics(completed)

        return {
            "scope": scope,
            "code": code,
            "eval_window_days": int(eval_window_days),
            "engine_version": engine_version,
            "total_evaluations": total,
            "completed_count": len(completed),
            "insufficient_count": insufficient_count,
            "long_count": long_count,
            "cash_count": cash_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "neutral_count": neutral_count,
            "direction_accuracy_pct": direction_accuracy_pct,
            "win_rate_pct": win_rate_pct,
            "neutral_rate_pct": neutral_rate_pct,
            "avg_stock_return_pct": avg_stock_return_pct,
            "avg_simulated_return_pct": avg_simulated_return_pct,
            # 基准（买入并持有）对照：判断建议是否跑赢"什么都不做"
            "benchmark_avg_return_pct": avg_stock_return_pct,
            "excess_vs_benchmark_pct": cls._excess_vs_benchmark(
                avg_simulated_return_pct, avg_stock_return_pct
            ),
            # 风险/质量指标：只看胜率会被"高胜率小亏低赚"误导
            "profit_factor": risk_metrics["profit_factor"],
            "max_drawdown_pct": risk_metrics["max_drawdown_pct"],
            "avg_win_return_pct": risk_metrics["avg_win_return_pct"],
            "avg_loss_return_pct": risk_metrics["avg_loss_return_pct"],
            "largest_loss_pct": risk_metrics["largest_loss_pct"],
            "largest_win_pct": risk_metrics["largest_win_pct"],
            "drawdown_series_points": risk_metrics["drawdown_series_points"],
            "stop_loss_trigger_rate": stop_loss_trigger_rate,
            "take_profit_trigger_rate": take_profit_trigger_rate,
            "ambiguous_rate": ambiguous_rate,
            "ambiguous_count": sum(
                1 for r in any_target_applicable if (r.first_hit or "") == "ambiguous"
            ),
            "avg_days_to_first_hit": avg_days_to_first_hit,
            "advice_breakdown": advice_breakdown,
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _excess_vs_benchmark(
        avg_simulated_return_pct: Optional[float],
        avg_stock_return_pct: Optional[float],
    ) -> Optional[float]:
        """Average excess of simulated performance over buy-and-hold."""
        if avg_simulated_return_pct is None or avg_stock_return_pct is None:
            return None
        return round(avg_simulated_return_pct - avg_stock_return_pct, 4)

    @classmethod
    def _compute_risk_metrics(cls, completed: List[BacktestResultLike]) -> Dict[str, Any]:
        """Profit factor and drawdown over the per-signal simulated returns.

        The series is the sequence of independent per-signal outcomes (not a
        portfolio equity curve), so ``max_drawdown_pct`` is an approximation used
        for outlier detection, not a strategy drawdown. Rows without an
        ``analysis_date`` cannot be ordered chronologically and are skipped for
        the drawdown series while still feeding the averages.
        """
        returns = [
            float(r.simulated_return_pct)
            for r in completed
            if r.simulated_return_pct is not None
        ]
        if not returns:
            return {
                "profit_factor": None,
                "max_drawdown_pct": None,
                "avg_win_return_pct": None,
                "avg_loss_return_pct": None,
                "largest_loss_pct": None,
                "largest_win_pct": None,
                "drawdown_series_points": 0,
            }

        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        avg_win = cls._average(wins)
        avg_loss = cls._average(losses)
        profit_factor: Optional[float]
        if avg_win is None:
            profit_factor = None
        elif avg_loss is None:
            # 没有亏损样本：盈亏比为无穷，置 None 并由调用方按"无亏损"解读
            profit_factor = None
        else:
            profit_factor = round(avg_win / abs(avg_loss), 4)

        dated = [
            (getattr(r, "analysis_date", None), float(r.simulated_return_pct))
            for r in completed
            if r.simulated_return_pct is not None and getattr(r, "analysis_date", None) is not None
        ]
        max_drawdown_pct: Optional[float] = None
        if dated:
            dated.sort(key=lambda item: item[0])
            equity = 1.0
            peak = 1.0
            worst = 0.0
            for _, value in dated:
                equity *= 1.0 + value / 100.0
                peak = max(peak, equity)
                if peak > 0:
                    worst = min(worst, (equity - peak) / peak * 100.0)
            max_drawdown_pct = round(worst, 4)

        return {
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_drawdown_pct,
            "avg_win_return_pct": avg_win,
            "avg_loss_return_pct": avg_loss,
            "largest_loss_pct": min(returns),
            "largest_win_pct": max(returns),
            "drawdown_series_points": len(dated),
        }

    @staticmethod
    def _normalize_text(value: Optional[str]) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _matches_intent(cls, text: str, keywords: Sequence[str]) -> bool:
        """Check if text expresses the intent of any keyword, accounting for negation.

        Tier 1: exact match (covers clean labels like "买入", "hold").
        Tier 2: substring match with negation guard.
        Keywords are assumed to be lowercase (matching _normalize_text output).
        """
        return cls._first_intent_position(text, keywords) is not None

    @classmethod
    def _first_intent_position(cls, text: str, keywords: Sequence[str]) -> Optional[int]:
        """Return the earliest match position for intent keywords, or None."""
        if not text:
            return None

        best_pos: Optional[int] = None

        for kw in keywords:
            if not kw:
                continue
            if text == kw:
                return 0

            keyword = kw.lower().strip()
            if not keyword:
                continue

            # Use word-boundary matching for ASCII keywords to avoid
            # false positives such as "watch" matching "wait".
            if bool(re.search(r"[a-z]", keyword)):
                for match in re.finditer(
                    rf"(?<![a-zA-Z0-9_]){re.escape(keyword)}(?![a-zA-Z0-9_])",
                    text,
                ):
                    if not cls._is_negated(text[: match.start()], keyword):
                        pos = match.start()
                        if best_pos is None or pos < best_pos:
                            best_pos = pos
                            break
                    continue

            # For non-ASCII terms (Chinese), use substring matching to keep
            # natural language phrasings like "建议买入" effective.
            if re.search(r"[\u4e00-\u9fff]", keyword):
                start = 0
                while True:
                    match_idx = text.find(keyword, start)
                    if match_idx < 0:
                        break
                    if not cls._is_negated(text[:match_idx], keyword):
                        if best_pos is None or match_idx < best_pos:
                            best_pos = match_idx
                        break
                    start = match_idx + len(keyword)
                continue

        return best_pos

    @classmethod
    def _is_negated(cls, prefix: str, keyword: str) -> bool:
        """Check if the prefix text indicates negation for a candidate intent."""
        stripped = prefix.rstrip()
        target = (keyword or "").lower().strip()
        if not target:
            return False

        if any(stripped.endswith(neg) for neg in cls._NEGATION_PATTERNS):
            return True

        # 限定“否定 + 动作动词”匹配，避免将“条件位否定”误伤核心建议意图。
        lookback = stripped[-12:]
        for neg in cls._NEGATION_PATTERNS:
            if not neg:
                continue
            neg_idx = lookback.rfind(neg)
            if neg_idx < 0:
                continue

            suffix_gap = lookback[neg_idx + len(neg):].strip()
            if not suffix_gap:
                return True
            if any(ch in suffix_gap for ch in "，,。；;:!?！？"):
                continue

            if cls._contains_keyword(suffix_gap, target):
                return True

            # Keep English short-gap behavior where negation words are followed by
            # connector words such as "to" (e.g. "not to sell").
            if not any(ch >= "\u4e00" and ch <= "\u9fff" for ch in suffix_gap):
                if len(suffix_gap) <= 6:
                    return True
                continue

            if cls._is_negation_connector_gap(suffix_gap):
                return True

        return False

    @classmethod
    def _contains_keyword(cls, text: str, keyword: str) -> bool:
        """Check whether *keyword* exists in text with intent-aware boundaries."""
        if not text or not keyword:
            return False
        if bool(re.search(r"[a-z]", keyword)):
            return bool(re.search(rf"(?<![a-zA-Z0-9_]){re.escape(keyword)}(?![a-zA-Z0-9_])", text))
        return keyword in text

    @classmethod
    def _is_negation_connector_gap(cls, gap: str) -> bool:
        """Whether a short Chinese negation gap is still a valid negation bridge."""
        compact = re.sub(r"[\s,，。；;:!?！？]", "", gap).strip()
        if not compact:
            return True
        return compact in cls._NEGATION_CONNECTOR_WORDS

    @classmethod
    def _classify_outcome(
        cls,
        *,
        stock_return_pct: Optional[float],
        direction_expected: str,
        neutral_band_pct: float,
    ) -> tuple[Optional[str], Optional[bool]]:
        if stock_return_pct is None:
            return None, None

        band = abs(float(neutral_band_pct))
        r = float(stock_return_pct)

        if direction_expected == "up":
            if r >= band:
                return "win", True
            if r <= -band:
                return "loss", False
            return "neutral", None

        if direction_expected == "down":
            if r <= -band:
                return "win", True
            if r >= band:
                return "loss", False
            return "neutral", None

        if direction_expected == "not_down":
            if r >= 0:
                return "win", True
            if r <= -band:
                return "loss", False
            return "neutral", None

        # flat
        if abs(r) <= band:
            return "win", True
        return "loss", False

    @classmethod
    def _classify_signal_outcome(
        cls,
        *,
        stock_return_pct: Optional[float],
        direction_expected: str,
        neutral_band_pct: float,
    ) -> tuple[Optional[str], Optional[bool]]:
        if stock_return_pct is None:
            return None, None

        band = abs(float(neutral_band_pct))
        r = float(stock_return_pct)

        if direction_expected == "up":
            if r >= band:
                return "hit", True
            if r <= -band:
                return "miss", False
            return "neutral", None

        if direction_expected == "not_down":
            if r >= 0:
                return "hit", True
            if r <= -band:
                return "miss", False
            return "neutral", None

        if direction_expected == "not_up":
            if r <= band:
                return "hit", True
            return "miss", False

        return None, None

    @staticmethod
    def _finite_optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _evaluate_targets(
        cls,
        *,
        position: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        window_bars: List[DailyBarLike],
        end_close: Optional[float],
    ) -> tuple[
        Optional[bool],
        Optional[bool],
        str,
        Optional[date],
        Optional[int],
        Optional[float],
        str,
    ]:
        if position != "long":
            return (
                None,
                None,
                "not_applicable",
                None,
                None,
                None,
                "cash",
            )

        has_any_target = stop_loss is not None or take_profit is not None
        if not has_any_target:
            return (
                None,
                None,
                "neither",
                None,
                None,
                end_close,
                "window_end",
            )

        hit_sl: Optional[bool] = None if stop_loss is None else False
        hit_tp: Optional[bool] = None if take_profit is None else False
        first_hit = "neither"
        first_hit_date: Optional[date] = None
        first_hit_days: Optional[int] = None
        exit_price: Optional[float] = end_close
        exit_reason = "window_end"

        for idx, bar in enumerate(window_bars, start=1):
            low = bar.low
            high = bar.high
            open_price = cls._finite_optional_float(getattr(bar, "open", None))
            stop_hit = stop_loss is not None and low is not None and low <= stop_loss
            tp_hit = take_profit is not None and high is not None and high >= take_profit

            if stop_hit:
                hit_sl = True
            if tp_hit:
                hit_tp = True

            if not stop_hit and not tp_hit:
                continue

            first_hit_date = bar.date
            first_hit_days = idx

            # 跳空保护：开盘已越过触发价时，成交价只能是开盘价。
            # 止损跳空 -> 按开盘（更差）；止盈跳空 -> 按开盘（更好）。
            stop_fill = stop_loss
            if stop_hit and open_price is not None and open_price < stop_loss:
                stop_fill = open_price
            take_fill = take_profit
            if tp_hit and open_price is not None and open_price > take_profit:
                take_fill = open_price

            if stop_hit and tp_hit:
                first_hit = "ambiguous"
                exit_price = stop_fill
                exit_reason = "ambiguous_stop_loss"
                break

            if stop_hit:
                first_hit = "stop_loss"
                exit_price = stop_fill
                exit_reason = "stop_loss"
                break

            first_hit = "take_profit"
            exit_price = take_fill
            exit_reason = "take_profit"
            break

        return (
            hit_sl,
            hit_tp,
            first_hit,
            first_hit_date,
            first_hit_days,
            exit_price,
            exit_reason,
        )

    @staticmethod
    def _average(values: Iterable[Optional[float]]) -> Optional[float]:
        items = [float(v) for v in values if v is not None]
        if not items:
            return None
        return round(sum(items) / len(items), 4)

    @staticmethod
    def _compute_advice_breakdown(results: List[BacktestResultLike]) -> Dict[str, Any]:
        breakdown: Dict[str, Dict[str, int]] = {}
        for row in results:
            raw_advice = row.operation_advice
            advice = (raw_advice if isinstance(raw_advice, str) else str(raw_advice or "")).strip() or "(unknown)"
            bucket = breakdown.setdefault(advice, {"total": 0, "win": 0, "loss": 0, "neutral": 0})
            bucket["total"] += 1
            outcome = (row.outcome or "").strip()
            if outcome in ("win", "loss", "neutral"):
                bucket[outcome] += 1

        enriched: Dict[str, Any] = {}
        for advice, bucket in breakdown.items():
            win = bucket["win"]
            loss = bucket["loss"]
            denom = win + loss
            win_rate = round(win / denom * 100, 2) if denom else None
            enriched[advice] = {**bucket, "win_rate_pct": win_rate}
        return enriched

    @staticmethod
    def _compute_diagnostics(results: List[BacktestResultLike]) -> Dict[str, Any]:
        status_counts: Dict[str, int] = {}
        first_hit_counts: Dict[str, int] = {}
        for row in results:
            status = (row.eval_status or "").strip() or "(unknown)"
            status_counts[status] = status_counts.get(status, 0) + 1
            first_hit = (row.first_hit or "").strip() or "(none)"
            first_hit_counts[first_hit] = first_hit_counts.get(first_hit, 0) + 1
        return {
            "eval_status": status_counts,
            "first_hit": first_hit_counts,
        }
