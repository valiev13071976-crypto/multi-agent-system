from datetime import datetime, timezone
import unittest

from fastapi.testclient import TestClient

from agents.model_router import REASON_ALL_AVAILABLE_PROVIDERS, ModelRouter
from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
from autonomy.errors import AutonomyDeniedError
from autonomy.gate import AutonomyGate, build_proposed_action, queue_side_effect_permitted
from autonomy.models import DECISION_ALLOW, DECISION_DENY, DECISION_REQUIRE_APPROVAL
from task_queue.queue import TaskQueue
from task_queue.worker import TaskWorker
from tests.test_mode_auto import STRATEGY_TEXT, load_auto_app
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_model_router import registry_with
from tests.test_smoke import CONTRACT_KEYS, load_app
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_READ_ONLY_EXTERNAL
from tools.search.fake_provider import FakeSearchProvider
from workflow.engine import WorkflowEngine
from workflow.models import STATUS_COMPLETED


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class AutonomyGateTests(unittest.TestCase):

    def test_deny_by_default_without_capabilities(self):
        gate = AutonomyGate()
        action = build_proposed_action(action_type="read")
        decision = gate.evaluate(action)
        self.assertEqual(decision.decision, DECISION_DENY)

    def test_queue_hook_allows_only_allow_decision(self):
        gate = AutonomyGate(autonomy_level="analyst")
        action = build_proposed_action(action_type="read")
        caps = CapabilitySet(
            subject_id="s",
            capabilities=(CAP_EXTERNAL_READ,),
            issued_at=T0,
        )
        allowed = gate.evaluate(action, capabilities=caps)
        self.assertTrue(queue_side_effect_permitted(allowed))
        denied = gate.evaluate(action)
        self.assertFalse(queue_side_effect_permitted(denied))
        worker = TaskWorker(TaskQueue())
        with self.assertRaises(AutonomyDeniedError):
            worker.require_autonomy_allow(denied)

    def test_as_analyze_still_seven_fields(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(len(payload), 7)
        self.assertEqual(payload["role"], "Judge")
        self.assertNotIn("autonomy_decision", payload)
        self.assertNotIn("approval", payload)
        self.assertNotIn("workflow_id", payload)
        self.assertNotIn("action_id", payload)

    def test_at_read_only_fact_check_path(self):
        fake = FakeSearchProvider({"WidgetIndex": ["https://oecd.org/a"]})
        main_mod = load_app(**env_for("openai"))
        main_mod.router.pipeline.fact_validator.gateway = ToolGateway(fake)
        self.assertEqual(
            main_mod.router.pipeline.fact_validator.gateway.tool_trust_level,
            TOOL_TRUST_READ_ONLY_EXTERNAL,
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": "Найди поставщика",
                    "mode": "openai",
                    "role": "researcher",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 7)

    def test_au_workflow_lifecycle_unchanged_for_analyze(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        workflow_id = main_mod.router.workflow_engine.last_workflow_id
        state = main_mod.router.workflow_engine.state_manager.get(workflow_id)
        self.assertEqual(state.status, STATUS_COMPLETED)

    def test_av_task_queue_behavior_unchanged(self):
        queue = TaskQueue()
        item = queue.enqueue(
            workflow_id="wf",
            task_id="t",
            execution_key="ek-av",
        )
        self.assertEqual(item.status, "queued")
        leased = queue.dequeue()
        self.assertEqual(leased.status, "leased")

    def test_aw_finops_429_unchanged(self):
        overrides = env_for("openai")
        overrides["FINOPS_UNKNOWN_COST_POLICY"] = "deny"
        main_mod = load_app(**overrides)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "openai"},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(mocks["openai"].await_count, 0)

    def test_ax_mode_both_unchanged(self):
        router = ModelRouter(registry_with("openai", "anthropic"))
        decision = router.decide(mode="both", role_id="strategist")
        self.assertEqual(decision.provider_ids, ("openai", "anthropic"))
        self.assertEqual(decision.reason, REASON_ALL_AVAILABLE_PROVIDERS)
        main_mod = load_app(**env_for("openai", "anthropic"))
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": "Найди поставщика", "mode": "both"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["openai"].await_count, 1)
        self.assertEqual(mocks["anthropic"].await_count, 1)

    def test_ay_mode_auto_unchanged(self):
        main_mod = load_auto_app("anthropic", "openai", auto_order="anthropic,openai")
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "anthropic", "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)

    def test_engine_default_has_no_side_effect_executor(self):
        engine = WorkflowEngine()
        self.assertIsNone(engine.autonomy_gate)
        engine._gate()
        self.assertIsNotNone(engine.autonomy_gate)


if __name__ == "__main__":
    unittest.main()
