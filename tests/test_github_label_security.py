import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from autonomy.capabilities import (
    CAP_CODE_EXECUTE,
    CAP_FINANCIAL_CHANGE,
    CAP_MESSAGE_SEND,
    CAP_PERMISSION_MANAGE,
    CAP_PRICING_WRITE,
    CAP_PURCHASE,
    CAP_SITE_WRITE,
)
from autonomy.gate import build_proposed_action
from side_effects.errors import SideEffectExecutionDeniedError, SideEffectExecutionError
from side_effects.factory import build_production_side_effect_registry
from side_effects.github.models import (
    GITHUB_TOOL_ID,
    GitHubTargetError,
    parse_github_label_resource,
)
from side_effects.github.transport import GitHubHttpTransport
from side_effects.models import SideEffectExecutionContext
from tests.test_github_write_config import DictSecrets
from tests.side_effect_fixtures import (
    T0,
    github_action,
    github_eval_kwargs,
    github_execute,
    github_runtime,
    issue_permit,
)
from tests.test_mode_auto import STRATEGY_TEXT, load_auto_app
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_READ_ONLY_EXTERNAL, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from task_queue.retry import is_retryable


class GitHubLabelSecurityTests(unittest.IsolatedAsyncioTestCase):

    def test_bd_disabled_registry_empty(self):
        registry = build_production_side_effect_registry(
            secrets=DictSecrets(),
            env={"GITHUB_WRITE_ADAPTER_ENABLED": "false"},
        )
        self.assertIsNone(registry.get(GITHUB_TOOL_ID))

    def test_bf_fake_not_production_default(self):
        registry = build_production_side_effect_registry(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_x"}),
            env={
                "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
            },
        )
        adapter = registry.get(GITHUB_TOOL_ID)
        self.assertIsInstance(adapter._transport, GitHubHttpTransport)

    def test_bg_env_example_placeholder(self):
        text = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("GITHUB_WRITE_ADAPTER_ENABLED=false", text)
        self.assertNotIn("GITHUB_WRITE_ADAPTER_ENABLED=true", text)

    def test_ba_resource_cannot_inject_url(self):
        with self.assertRaises(GitHubTargetError):
            parse_github_label_resource(
                "https://api.github.com/repos/octo/hello/issues/1/labels/bug"
            )

    def test_bb_owner_repo_cannot_redirect_host(self):
        with self.assertRaises(GitHubTargetError):
            parse_github_label_resource("github://github.com/octo/issues/1/labels/bug")

    def test_bc_label_control_characters_rejected(self):
        with self.assertRaises(GitHubTargetError):
            parse_github_label_resource("github://octo/hello/issues/1/labels/bu\ng")

    def test_bj_analyze_still_seven_fields(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            payload = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "openai"},
            ).json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(len(CONTRACT_KEYS), 7)

    def test_br_mode_auto_unchanged(self):
        main_mod = load_auto_app("anthropic", "openai", auto_order="anthropic,openai")
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "anthropic")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": STRATEGY_TEXT, "mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json().keys()), set(CONTRACT_KEYS))

    def test_bs_mode_both_unchanged(self):
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
        self.assertEqual(set(response.json().keys()), set(CONTRACT_KEYS))

    def test_bl_tool_gateway_read_only(self):
        gateway = ToolGateway()
        self.assertEqual(gateway.tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)

    async def test_av_token_absent_from_records(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.seed("octo", "hello", 1, [])
        action = github_action(workflow_id, idempotency_key="sec-token")
        result = await github_execute(executor, action, engine)
        blob = repr(result) + str(dict(result.metadata))
        blob += str(executor.store.get(result.execution_id))
        for event in executor.audit.events():
            blob += str(event) + str(dict(event.metadata))
        self.assertNotIn("ghs_", blob)
        self.assertNotIn("Authorization", blob)
        self.assertNotIn("Bearer ", blob)

    async def test_au_raw_github_body_absent(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        fake.get_status = 401
        action = github_action(workflow_id)
        with self.assertRaises(SideEffectExecutionError) as caught:
            await github_execute(executor, action, engine)
        self.assertEqual(str(caught.exception), "github_authentication_failed")

    async def test_disabled_action_regression(self):
        engine, workflow_id, adapter, executor, fake = github_runtime()
        cases = (
            ("purchase", (CAP_PURCHASE,)),
            ("financial_change", (CAP_FINANCIAL_CHANGE,)),
            ("send_message", (CAP_MESSAGE_SEND,)),
            ("external_publish", (CAP_SITE_WRITE,)),
            ("permission_change", (CAP_PERMISSION_MANAGE,)),
            ("delete", (CAP_SITE_WRITE,)),
            ("execute_code", (CAP_CODE_EXECUTE,)),
        )
        for action_type, caps in cases:
            action = build_proposed_action(
                action_type=action_type,
                workflow_id=workflow_id,
                task_id="task-se",
                tool_id=GITHUB_TOOL_ID,
                operation="ensure_label_present",
                resource="github://octo/hello/issues/1/labels/bug",
                idempotency_key=f"dis-{action_type}",
                metadata={"reversible": True},
                tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
                requested_capabilities=caps,
            )
            permit = await issue_permit(engine, action)
            with self.assertRaises(SideEffectExecutionDeniedError):
                await executor.execute(
                    action,
                    permit=permit,
                    context=SideEffectExecutionContext(now=T0),
                    gate=engine._gate(),
                    hitl=engine._hitl(),
                    state_manager=engine.state_manager,
                    evaluate_kwargs=github_eval_kwargs(capabilities=caps),
                )
        self.assertEqual(fake.get_calls, 0)

        write_pricing = github_action(
            workflow_id,
            requested_capabilities=(CAP_PRICING_WRITE,),
            idempotency_key="dis-pricing",
        )
        permit = await issue_permit(engine, write_pricing)
        with self.assertRaises(SideEffectExecutionDeniedError):
            await executor.execute(
                write_pricing,
                permit=permit,
                context=SideEffectExecutionContext(now=T0),
                gate=engine._gate(),
                hitl=engine._hitl(),
                state_manager=engine.state_manager,
                evaluate_kwargs=github_eval_kwargs(capabilities=(CAP_PRICING_WRITE,)),
            )

    def test_bp_side_effect_codes_not_retryable(self):
        self.assertFalse(is_retryable("github_rate_limited"))
        self.assertFalse(is_retryable("external_write_timeout_uncertain"))
        self.assertFalse(is_retryable("execution_outcome_uncertain"))
