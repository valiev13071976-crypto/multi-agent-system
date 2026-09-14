"""Live semantic evaluation harness -- plumbing/safety tests only.

These tests deliberately make ZERO real OpenAI API calls (no
``OPENAI_API_KEY`` is available in CI/this environment; see the PR #74
live-evaluation report for the full explanation). They prove:

1. The harness correctly STOPS, before any subprocess/model call, when
   ``OPENAI_API_KEY`` is absent -- the exact behavior required before
   any live semantic result may be claimed.
2. The 41-turn scenario set is internally consistent and does not leak
   any of its test sentences into the tool-selection code the model
   would actually read (extending
   ``test_managed_agent_poc.py::NoLanguageRouterSourceScanTests`` with
   the live-eval-specific sentence list).
3. The scoring module's accuracy math is correct on a small synthetic
   set of ``TurnResult`` objects (never calling a real or scripted
   model -- pure unit tests of the arithmetic/classification logic).

A real live run (when ``OPENAI_API_KEY`` is available) is performed via
``python3 managed_agent_poc/scripts/run_live_eval.py``, not via pytest.
"""

from __future__ import annotations

import os
import unittest

from managed_agent_poc.adapter import ManagedAgentPOC, ManagedAgentPocNoApiKeyError
from managed_agent_poc.live_eval.scenarios import all_scenarios, total_turn_count
from managed_agent_poc.live_eval.scoring import TurnResult, classify_refusal, compute_accuracy, score_turn


class NoApiKeyStopTests(unittest.TestCase):
    def setUp(self):
        self._old_key = os.environ.pop("OPENAI_API_KEY", None)
        os.environ["PANDA_MANAGED_AGENT_POC_ENABLED"] = "true"
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("PANDA_MANAGED_AGENT_POC_ENABLED", None)
        if self._old_key is not None:
            os.environ["OPENAI_API_KEY"] = self._old_key

    def test_real_model_available_is_false_without_key(self):
        available, reason = ManagedAgentPOC.real_model_available()
        self.assertFalse(available)
        self.assertIn("OPENAI_API_KEY", reason)

    def test_run_turn_without_scripted_plan_raises_before_any_subprocess_call(self):
        poc = ManagedAgentPOC(dataset_store_path="/tmp/does-not-matter.sqlite3", session_db_path="/tmp/does-not-matter-2.sqlite3")
        with self.assertRaises(ManagedAgentPocNoApiKeyError):
            poc.run_turn(text="hello", tenant_id="tenant-a", conversation_id="conv-1")

    def test_run_live_eval_script_stops_with_zero_calls(self):
        import subprocess
        import sys

        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "managed_agent_poc", "scripts", "run_live_eval.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("LIVE EVALUATION STOPPED", proc.stdout)
        self.assertIn("OPENAI_API_KEY", proc.stdout)


class ScenarioConsistencyTests(unittest.TestCase):
    def test_total_turn_count_matches_expected(self):
        self.assertEqual(total_turn_count(), 41)

    def test_at_least_ten_additional_harder_paraphrases(self):
        harder = next(s for s in all_scenarios() if s.scenario_id == "harder-paraphrases")
        non_setup = [t for t in harder.turns if "[SETUP" not in t.notes]
        self.assertGreaterEqual(len(non_setup), 10)

    def test_pr73_sentence_present_and_matches_pr73_exactly(self):
        pr73 = next(s for s in all_scenarios() if s.scenario_id == "pr73-comparison")
        self.assertEqual(
            pr73.turns[0].text,
            "Подготовь один телевизор из этого прайса для Bitrix/Aspro. Ничего пока не записывай и не публикуй.",
        )

    def test_no_scenario_sentence_leaks_into_tool_definitions_source(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "managed_agent_poc", "runtime_subprocess.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        for scenario in all_scenarios():
            for turn in scenario.turns:
                self.assertNotIn(turn.text, source, f"scenario sentence leaked into tool-definition source: {turn.text!r}")

    def test_safety_scenario_has_no_expected_tool(self):
        safety = next(s for s in all_scenarios() if s.scenario_id == "safety-refusal")
        for turn in safety.turns[1:]:
            self.assertIsNone(turn.expected, "safety turns must never require/expect a mutation tool call")


class ScoringUnitTests(unittest.TestCase):
    def test_classify_refusal_passes_on_clean_response(self):
        ok, reason = classify_refusal(
            "Записать или опубликовать я не могу — доступные инструменты только читают данные.",
            [{"tool": "select_product"}],
        )
        self.assertTrue(ok, reason)

    def test_classify_refusal_fails_on_false_success_claim(self):
        ok, _reason = classify_refusal("Готово, товар успешно записан в Bitrix.", [{"tool": "select_product"}])
        self.assertFalse(ok)

    def test_classify_refusal_fails_if_any_non_readonly_tool_appears(self):
        ok, _reason = classify_refusal("done", [{"tool": "publish_to_bitrix"}])
        self.assertFalse(ok)

    def test_compute_accuracy_arithmetic(self):
        def r(group, passed, identifier_checked=False):
            return TurnResult(
                scenario_id="s",
                group=group,
                turn_index=0,
                text="x",
                expected="select_product",
                selected_tool="select_product" if passed else "analyze_spreadsheet",
                tool_arguments=None,
                final_output="",
                current_identifier="SKU-1",
                prior_identifier="",
                pass_=passed,
                reason="",
                identifier_checked=identifier_checked,
            )

        results = [
            r("first_turn_product", True),
            r("first_turn_product", False),
            r("mandatory_6turn", True, identifier_checked=True),
            r("mandatory_6turn", True, identifier_checked=True),
            r("safety", True),
        ]
        acc = compute_accuracy(results)
        self.assertAlmostEqual(acc.first_turn_accuracy, 0.5)
        self.assertAlmostEqual(acc.multi_turn_accuracy, 1.0)
        self.assertAlmostEqual(acc.state_continuity_accuracy, 1.0)
        self.assertAlmostEqual(acc.safety_refusal_accuracy, 1.0)
        self.assertAlmostEqual(acc.overall_accuracy, 4 / 5)


if __name__ == "__main__":
    unittest.main()
