from fastapi.testclient import TestClient
import unittest

from agents.judge import Judge
from agents.validators.confidence import CONFIDENCE_MAX, CONFIDENCE_MIN, compute_confidence
from agents.validators.models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    ConfidenceInputs,
    ValidationResult,
)
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


def _inputs(**overrides):
    values = dict(
        successful_experts=1,
        failed_providers=0,
        structural_fail=False,
        structural_all_pass=True,
        consistency_status=STATUS_UNKNOWN,
        sources_present=False,
        factual_status=STATUS_UNKNOWN,
        category="strategy",
    )
    values.update(overrides)
    return ConfidenceInputs(**values)


def _structural(status):
    return ValidationResult(
        validator_id="structural",
        status=status,
        score=1.0 if status == STATUS_PASS else 0.0,
        issues=(),
        evidence={},
        reason="meaningful_text" if status == STATUS_PASS else "empty_answer",
    )


class JudgeV2Tests(unittest.IsolatedAsyncioTestCase):

    def test_g_research_without_sources_is_lower_than_strategy(self):
        strategy = compute_confidence(_inputs(category="strategy"))
        research = compute_confidence(_inputs(category="research"))
        self.assertLess(research, strategy)

    def test_h_provider_failure_lowers_confidence(self):
        healthy = compute_confidence(_inputs(failed_providers=0))
        failed = compute_confidence(_inputs(failed_providers=1))
        self.assertLess(failed, healthy)

    def test_i_two_successful_experts_raise_confidence(self):
        one = compute_confidence(_inputs(successful_experts=1))
        two = compute_confidence(_inputs(successful_experts=2))
        self.assertGreater(two, one)

    def test_j_structural_fail_lowers_confidence_substantially(self):
        ok = compute_confidence(_inputs(structural_fail=False, structural_all_pass=True))
        bad = compute_confidence(_inputs(structural_fail=True, structural_all_pass=False))
        self.assertLessEqual(bad, round(ok - 0.20, 2))

    def test_k_confidence_is_deterministic(self):
        first = compute_confidence(_inputs(successful_experts=2, category="research"))
        second = compute_confidence(_inputs(successful_experts=2, category="research"))
        self.assertEqual(first, second)

    def test_l_confidence_is_clamped(self):
        low = compute_confidence(
            _inputs(
                successful_experts=0,
                failed_providers=3,
                structural_fail=True,
                structural_all_pass=False,
                consistency_status=STATUS_FAIL,
                category="research",
            )
        )
        high = compute_confidence(
            _inputs(
                successful_experts=4,
                structural_all_pass=True,
                consistency_status=STATUS_PASS,
                sources_present=True,
                factual_status=STATUS_PASS,
                category="strategy",
            )
        )
        self.assertGreaterEqual(low, CONFIDENCE_MIN)
        self.assertLessEqual(high, CONFIDENCE_MAX)
        self.assertGreaterEqual(high, CONFIDENCE_MIN)

    def test_m_provider_name_does_not_change_confidence(self):
        openai_score = compute_confidence(_inputs())
        anthropic_score = compute_confidence(_inputs())
        self.assertEqual(openai_score, anthropic_score)

    async def test_m_judge_is_provider_neutral(self):
        judge = Judge()
        structural = {"openai": _structural(STATUS_PASS)}
        other = {"anthropic": _structural(STATUS_PASS)}
        fact = ValidationResult(
            validator_id="fact",
            status=STATUS_UNKNOWN,
            score=0.0,
            issues=("no_external_evidence",),
            evidence={"sources_present": False, "evidence_available": False},
            reason="no_external_evidence",
        )
        left = await judge.run(
            experts={"openai": "Shared expert conclusion."},
            fact_report=fact,
            structural=structural,
            consistency=ValidationResult(
                validator_id="consistency",
                status=STATUS_UNKNOWN,
                score=0.0,
                issues=(),
                evidence={},
                reason="insufficient_answers",
            ),
            category="strategy",
        )
        right = await judge.run(
            experts={"anthropic": "Shared expert conclusion."},
            fact_report=fact,
            structural=other,
            consistency=ValidationResult(
                validator_id="consistency",
                status=STATUS_UNKNOWN,
                score=0.0,
                issues=(),
                evidence={},
                reason="insufficient_answers",
            ),
            category="strategy",
        )
        self.assertEqual(left["confidence"], right["confidence"])
        self.assertEqual(left["best_solution"], right["best_solution"])

    def test_p_success_response_contract(self):
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "both"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(len(payload), 7)
        self.assertEqual(payload["role"], "Judge")
        self.assertGreaterEqual(payload["confidence"], CONFIDENCE_MIN)
        self.assertLessEqual(payload["confidence"], CONFIDENCE_MAX)
        self.assertIn("successful openai answer", payload["analysis"])
        self.assertNotIn("validation", payload)


if __name__ == "__main__":
    unittest.main()
