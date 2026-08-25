from dataclasses import FrozenInstanceError
import unittest

from agents.fact_validator import FactValidator
from agents.peer_review import PeerReview
from agents.validators.consistency import ConsistencyValidator
from agents.validators.models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    ValidationResult,
)
from agents.validators.structural import StructuralValidator
from security.redaction import REDACTED, redact


class ValidatorTests(unittest.IsolatedAsyncioTestCase):

    def test_validation_result_is_immutable(self):
        result = ValidationResult(
            validator_id="structural",
            status=STATUS_PASS,
            score=1.0,
            issues=(),
            evidence={"char_count": 4},
            reason="meaningful_text",
        )
        with self.assertRaises(FrozenInstanceError):
            result.status = STATUS_FAIL

    def test_a_empty_answer_is_structural_fail(self):
        result = StructuralValidator().validate("   ")
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertEqual(result.reason, "empty_answer")

    def test_b_normal_text_is_structural_pass(self):
        result = StructuralValidator().validate("Нормальный экспертный ответ про рынок.")
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.reason, "meaningful_text")

    def test_c_identical_answers_are_consistency_pass(self):
        result = ConsistencyValidator().validate(
            {
                "openai": "Same expert conclusion.",
                "anthropic": "Same expert conclusion.",
            }
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.reason, "exact_agreement")

    def test_d_direct_contradiction_is_consistency_fail(self):
        result = ConsistencyValidator().validate(
            {
                "openai": "CONSENSUS: YES. The proposal should proceed.",
                "anthropic": "CONSENSUS: NO. The proposal should not proceed.",
            }
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertEqual(result.reason, "direct_contradiction")

    def test_e_unproven_difference_is_unknown_not_fake_pass(self):
        result = ConsistencyValidator().validate(
            {
                "openai": "The warehouse should be moved closer to the port.",
                "anthropic": "Hiring seasonal staff may reduce overtime costs.",
            }
        )
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.reason, "no_reliable_signal")
        self.assertNotEqual(result.status, STATUS_PASS)

    async def test_f_fact_validator_without_external_evidence(self):
        result = await FactValidator().validate(
            {"openai": "Цена выросла на 12% в прошлом квартале."}
        )
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.reason, "no_external_evidence")
        self.assertFalse(result.evidence["evidence_available"])
        self.assertNotIn("facts verified", result.reason)

    async def test_n_issues_are_machine_safe_and_redacted(self):
        secret = "sk-validator-leak-secret"
        peer = await PeerReview().review(
            {"openai": "ok answer"},
            errors={"anthropic": {"type": "RuntimeError", "message": secret}},
        )
        dumped = str(peer.issues) + str(dict(peer.evidence))
        self.assertNotIn(secret, dumped)
        self.assertIn("missing_provider:anthropic", peer.issues)
        rendered = redact(f"Authorization: Bearer {secret}", extra_secrets=(secret,))
        self.assertNotIn(secret, rendered)
        self.assertIn(REDACTED, rendered)

    def test_placeholder_and_error_only_fail_structurally(self):
        structural = StructuralValidator()
        self.assertEqual(structural.validate("TODO").reason, "placeholder_answer")
        self.assertEqual(
            structural.validate("ValueError: boom").reason,
            "error_only_answer",
        )


if __name__ == "__main__":
    unittest.main()
