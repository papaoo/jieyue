# -*- coding: utf-8 -*-
"""研判闭环链路测试。

1. LLM 输出解析失败时不再伪造买入/卖出评级（回到 canonical 中性档）
2. 常驻进程会自动触发 skill / 决策信号的事后结果评估（闭环回流）
"""

from __future__ import annotations

import unittest
from unittest import mock

from src.services.runtime_scheduler import (
    _outcome_evaluation_interval_seconds,
    build_outcome_evaluation_background_tasks,
)


class OutcomeEvaluationTaskTestCase(unittest.TestCase):
    def test_task_is_registered_by_default(self) -> None:
        config = mock.MagicMock()
        config.outcome_evaluation_enabled = True
        config.outcome_evaluation_interval_minutes = 60

        tasks = build_outcome_evaluation_background_tasks(config)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "outcome_evaluation")
        self.assertEqual(tasks[0]["interval_seconds"], 3600)
        self.assertTrue(tasks[0]["run_immediately"])

    def test_task_can_be_disabled(self) -> None:
        config = mock.MagicMock()
        config.outcome_evaluation_enabled = False

        self.assertEqual(build_outcome_evaluation_background_tasks(config), [])

    def test_invalid_interval_falls_back_to_one_hour(self) -> None:
        config = mock.MagicMock()
        config.outcome_evaluation_interval_minutes = "not-a-number"

        self.assertEqual(_outcome_evaluation_interval_seconds(config), 3600)

    def test_task_invokes_both_outcome_services(self) -> None:
        config = mock.MagicMock()
        config.outcome_evaluation_enabled = True
        config.outcome_evaluation_interval_minutes = 60

        tasks = build_outcome_evaluation_background_tasks(config)
        task = tasks[0]["task"]

        skill_instance = mock.MagicMock()
        skill_instance.run_outcomes.return_value = {"evaluated": 3}
        signal_instance = mock.MagicMock()
        signal_instance.run_outcomes.return_value = {"evaluated": 2}

        with mock.patch(
            "src.services.skill_opinion_outcome_service.SkillOpinionOutcomeService",
            return_value=skill_instance,
        ), mock.patch(
            "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService",
            return_value=signal_instance,
        ):
            task()

        # 闭环的关键：评估必须真的被调用，权重回流才可能生效
        skill_instance.run_outcomes.assert_called_once()
        signal_instance.run_outcomes.assert_called_once()

    def test_task_survives_service_failure(self) -> None:
        config = mock.MagicMock()
        config.outcome_evaluation_enabled = True
        config.outcome_evaluation_interval_minutes = 60

        task = build_outcome_evaluation_background_tasks(config)[0]["task"]

        failing = mock.MagicMock()
        failing.run_outcomes.side_effect = RuntimeError("boom")
        working = mock.MagicMock()
        working.run_outcomes.return_value = {"evaluated": 1}

        with mock.patch(
            "src.services.skill_opinion_outcome_service.SkillOpinionOutcomeService",
            return_value=failing,
        ), mock.patch(
            "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService",
            return_value=working,
        ):
            task()  # 单侧失败不得影响另一侧，也不得抛出

        working.run_outcomes.assert_called_once()


class ParseFailureNeutralRatingTestCase(unittest.TestCase):
    """解析失败必须回到中性档，而不是用词频伪造方向。"""

    def _analyzer(self):
        from src.analyzer import GeminiAnalyzer

        return GeminiAnalyzer.__new__(GeminiAnalyzer)

    def _runtime_config(self):
        cfg = mock.MagicMock()
        cfg.report_language = "zh"
        return cfg

    def test_bullish_wording_does_not_fabricate_buy_rating(self) -> None:
        analyzer = self._analyzer()
        with mock.patch.object(
            analyzer, "_get_runtime_config", return_value=self._runtime_config()
        ):
            result = analyzer._parse_text_response(
                "【看多】建议买入，目标价 120 元，逢低加仓。", "600519", "贵州茅台"
            )

        # 解析失败 = 没有结论：评分保持 canonical 中性 50、decision_type=hold
        self.assertEqual(result.sentiment_score, 50)
        self.assertEqual(result.decision_type, "hold")
        self.assertFalse(result.success)
        self.assertIn("no rating derived", result.error_message)

    def test_negated_wording_is_not_read_as_bullish_or_bearish(self) -> None:
        analyzer = self._analyzer()
        with mock.patch.object(
            analyzer, "_get_runtime_config", return_value=self._runtime_config()
        ):
            result = analyzer._parse_text_response(
                "当前不建议买入，追高风险大。", "600519", "贵州茅台"
            )

        self.assertEqual(result.sentiment_score, 50)
        self.assertEqual(result.decision_type, "hold")
        self.assertNotEqual(result.decision_type, "buy")
        self.assertNotEqual(result.decision_type, "sell")


if __name__ == "__main__":
    unittest.main()