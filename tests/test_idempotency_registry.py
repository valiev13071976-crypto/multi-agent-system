import unittest

from autonomy.errors import IdempotencyConflictError
from autonomy.capabilities import CAP_EXTERNAL_WRITE, CapabilitySet
from autonomy.gate import AutonomyGate, build_proposed_action
from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import DECISION_DENY, IDEMPOTENCY_COMPLETED, IDEMPOTENCY_RESERVED
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


T0_CAPS = CapabilitySet(
    subject_id="agent-1",
    capabilities=(CAP_EXTERNAL_WRITE,),
    issued_at=__import__("datetime").datetime(2026, 8, 25, tzinfo=__import__("datetime").timezone.utc),
)


class IdempotencyRegistryTests(unittest.TestCase):

    def test_aa_protected_write_without_key_denies(self):
        gate = AutonomyGate(autonomy_level="executor_bounded")
        action = build_proposed_action(
            action_type="write",
            idempotency_key=None,
            metadata={"reversible": True},
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
        )
        decision = gate.evaluate(action, capabilities=T0_CAPS)
        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "idempotency_required")

    def test_ab_reserve_new_key(self):
        registry = IdempotencyRegistry()
        record = registry.reserve("key-1", "action-1")
        self.assertEqual(record.state, IDEMPOTENCY_RESERVED)
        self.assertEqual(record.action_id, "action-1")

    def test_ac_duplicate_active_key_no_second_execution(self):
        registry = IdempotencyRegistry()
        registry.reserve("key-1", "action-1")
        with self.assertRaises(IdempotencyConflictError) as ctx:
            registry.reserve("key-1", "action-2")
        self.assertEqual(ctx.exception.reason_code, "duplicate_active")

    def test_ad_duplicate_completed_key_no_rerun(self):
        registry = IdempotencyRegistry()
        registry.reserve("key-1", "action-1")
        registry.mark_started("key-1")
        registry.mark_completed("key-1")
        with self.assertRaises(IdempotencyConflictError) as ctx:
            registry.reserve("key-1", "action-2")
        self.assertEqual(ctx.exception.reason_code, "duplicate_completed")

    def test_ae_completed_key_persists(self):
        registry = IdempotencyRegistry()
        registry.reserve("key-1", "action-1")
        registry.mark_completed("key-1")
        found = registry.get("key-1")
        self.assertEqual(found.state, IDEMPOTENCY_COMPLETED)
        self.assertEqual(found.action_id, "action-1")

    def test_af_idempotency_metadata_has_no_prompt_or_secret(self):
        registry = IdempotencyRegistry()
        record = registry.reserve(
            "key-1",
            "action-1",
            metadata={
                "prompt": "full prompt",
                "api_key": "sk-live",
                "tool_id": "crm",
            },
        )
        blob = str(dict(record.metadata))
        self.assertNotIn("full prompt", blob)
        self.assertNotIn("sk-live", blob)
        self.assertEqual(record.metadata.get("tool_id"), "crm")

    def test_gate_duplicate_completed_denies_second_action(self):
        gate = AutonomyGate(autonomy_level="executor_bounded")
        first = build_proposed_action(
            action_type="write",
            action_id="a1",
            idempotency_key="same-key",
            metadata={"reversible": True},
            tool_trust_level="INTERNAL_SAFE",
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            risk_class="low",
        )
        second = build_proposed_action(
            action_type="write",
            action_id="a2",
            idempotency_key="same-key",
            metadata={"reversible": True},
            tool_trust_level="INTERNAL_SAFE",
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            risk_class="low",
        )
        first_decision = gate.evaluate(first, capabilities=T0_CAPS)
        self.assertIn(first_decision.decision, {"allow", "review_after"})
        gate.idempotency.mark_completed("same-key")
        second_decision = gate.evaluate(second, capabilities=T0_CAPS)
        self.assertEqual(second_decision.decision, DECISION_DENY)
        self.assertEqual(second_decision.reason_code, "duplicate_completed")


if __name__ == "__main__":
    unittest.main()
