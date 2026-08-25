from fastapi.testclient import TestClient
import unittest

from tools.gateway import ToolGateway
from tools.search.fake_provider import FakeSearchProvider
from workflow.engine import WorkflowEngine
from workflow.models import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_WAITING_APPROVAL,
    STEP_COMPLETED,
)
from workflow.state_manager import StateManager
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


CLAIM = "The WidgetIndex reached 12.5% in 2024."


class WorkflowEngineUnitTests(unittest.IsolatedAsyncioTestCase):

    async def test_g_completed_step_is_not_rerun_on_resume(self):
        manager = StateManager(step_names=("one", "two"))
        engine = WorkflowEngine(state_manager=manager, step_names=("one", "two"))
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.start_step(state.workflow_id, "one")
        manager.complete_step(state.workflow_id, "one")
        ran = []

        async def one():
            ran.append("one")

        async def two():
            ran.append("two")

        result = await engine.resume(
            state.workflow_id,
            handlers={"one": one, "two": two},
        )
        self.assertNotIn("one", ran)
        self.assertEqual(ran, ["two"])
        self.assertEqual(result["executed"], ["two"])

    async def test_j_resume_non_terminal_continues(self):
        manager = StateManager(step_names=("one", "two"))
        engine = WorkflowEngine(state_manager=manager, step_names=("one", "two"))
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        result = await engine.resume(
            state.workflow_id,
            handlers={"one": _noop, "two": _noop},
        )
        self.assertTrue(result["ran"])
        self.assertEqual(manager.get(state.workflow_id).status, STATUS_COMPLETED)

    async def test_k_resume_terminal_does_not_execute(self):
        manager = StateManager(step_names=("one",))
        engine = WorkflowEngine(state_manager=manager, step_names=("one",))
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.complete_workflow(state.workflow_id)
        ran = []

        async def one():
            ran.append("one")

        result = await engine.resume(state.workflow_id, handlers={"one": one})
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "terminal")
        self.assertEqual(ran, [])

    async def test_l_waiting_approval_blocks_protected_step(self):
        manager = StateManager(step_names=("one", "two"))
        engine = WorkflowEngine(
            state_manager=manager,
            step_names=("one", "two"),
            protected_steps=frozenset({"two"}),
        )
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.start_step(state.workflow_id, "two")
        manager.wait_for_approval(state.workflow_id)
        self.assertEqual(manager.get(state.workflow_id).status, STATUS_WAITING_APPROVAL)
        ran = []

        async def two():
            ran.append("two")

        result = await engine.resume(state.workflow_id, handlers={"two": two})
        self.assertEqual(ran, [])
        self.assertEqual(result["reason"], "waiting_approval")

    async def test_m_approve_allows_resume_to_running(self):
        manager = StateManager(step_names=("one", "two"))
        engine = WorkflowEngine(
            state_manager=manager,
            step_names=("one", "two"),
            protected_steps=frozenset({"two"}),
        )
        state = manager.create(task_id="t")
        manager.plan(state.workflow_id)
        manager.start(state.workflow_id)
        manager.start_step(state.workflow_id, "two")
        manager.wait_for_approval(state.workflow_id)
        manager.approve(state.workflow_id)
        self.assertEqual(manager.get(state.workflow_id).status, "running")
        ran = []

        async def two():
            ran.append("two")

        result = await engine.resume(state.workflow_id, handlers={"two": two})
        self.assertEqual(ran, ["two"])
        self.assertTrue(result["ran"])


async def _noop():
    return None


class WorkflowEngineHttpTests(unittest.TestCase):

    def test_o_success_response_still_seven_fields(self):
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
        self.assertNotIn("workflow_id", payload)

    def test_p_successful_analyze_completes_workflow(self):
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
        self.assertEqual(state.task_id, main_mod.router.last_task_id)
        for name in (
            "prepare_context",
            "route",
            "execute_experts",
            "validate",
            "judge",
            "format",
        ):
            self.assertEqual(state.step(name).status, STEP_COMPLETED)

    def test_q_routing_error_fails_workflow_keeps_http(self):
        main_mod = load_app(**env_for("openai"))
        client = TestClient(main_mod.app)
        response = client.post(
            "/api/analyze",
            json={"prompt": "Найди поставщика", "mode": "not-a-provider"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error"], "invalid_mode")
        workflow_id = main_mod.router.workflow_engine.last_workflow_id
        state = main_mod.router.workflow_engine.state_manager.get(workflow_id)
        self.assertEqual(state.status, STATUS_FAILED)
        self.assertEqual(state.error_code, "invalid_mode")

    def test_r_finops_429_fails_workflow_keeps_http(self):
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
        workflow_id = main_mod.router.workflow_engine.last_workflow_id
        state = main_mod.router.workflow_engine.state_manager.get(workflow_id)
        self.assertEqual(state.status, STATUS_FAILED)
        self.assertEqual(state.error_code, "finops_budget_denied")

    def test_s_fact_check_timeout_still_completes(self):
        fake = FakeSearchProvider({"WidgetIndex": []})
        fake.delay_seconds = 1
        main_mod = load_app(**env_for("openai"))
        main_mod.router.pipeline.fact_validator.gateway = ToolGateway(
            fake,
            timeout_seconds=0.01,
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        mocks["openai"].return_value = CLAIM
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
        workflow_id = main_mod.router.workflow_engine.last_workflow_id
        state = main_mod.router.workflow_engine.state_manager.get(workflow_id)
        self.assertEqual(state.status, STATUS_COMPLETED)
        self.assertEqual(
            main_mod.router.pipeline.last_validation["fact"].reason,
            "external_evidence_timeout",
        )

    def test_t_mode_both_unchanged(self):
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
        self.assertEqual(set(response.json().keys()), set(CONTRACT_KEYS))

    def test_u_mode_auto_unchanged(self):
        overrides = env_for("openai", "anthropic")
        overrides["AUTO_PROVIDER_ORDER"] = "anthropic,openai"
        overrides["OPENAI_TASK_CATEGORIES"] = "general,technical"
        overrides["ANTHROPIC_TASK_CATEGORIES"] = "general,technical"
        main_mod = load_app(**overrides)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai", "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={
                    "prompt": "Traceback TypeError app.py",
                    "mode": "auto",
                    "role": "technical",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks["anthropic"].await_count, 1)
        self.assertEqual(mocks["openai"].await_count, 0)
        self.assertEqual(response.json()["role"], "Judge")


if __name__ == "__main__":
    unittest.main()
