import unittest

from fastapi.testclient import TestClient

from side_effects.github.models import GITHUB_TOOL_ID
from side_effects.github.transport import FakeGitHubTransport, GitHubHttpTransport
from side_effects.runtime import compose_side_effect_runtime
from commerce.product_platform.side_effect import COMMERCE_WRITE_TOOLS
from seo_marketing.side_effect import SEO_WRITE_TOOLS
from b2b_commerce.side_effect import B2B_WRITE_TOOLS
from b2b_commerce.side_effect import B2B_WRITE_TOOLS
from tests.test_github_write_config import DictSecrets
from tests.test_mode_auto import STRATEGY_TEXT, load_auto_app
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_READ_ONLY_EXTERNAL


class SideEffectRuntimeWiringTests(unittest.TestCase):

    def test_ag_default_zero_write_adapters(self):
        runtime = compose_side_effect_runtime(secrets=DictSecrets(), env={})
        self.assertIsNone(runtime.registry.get(GITHUB_TOOL_ID))
        for spec in COMMERCE_WRITE_TOOLS:
            self.assertIsNotNone(
                runtime.registry.get(spec["tool_id"]),
                msg=f"missing commerce side-effect adapter for {spec['tool_id']}",
            )
        for spec in SEO_WRITE_TOOLS:
            self.assertIsNotNone(
                runtime.registry.get(spec["tool_id"]),
                msg=f"missing seo side-effect adapter for {spec['tool_id']}",
            )
        for spec in B2B_WRITE_TOOLS:
            self.assertIsNotNone(
                runtime.registry.get(spec["tool_id"]),
                msg=f"missing b2b side-effect adapter for {spec['tool_id']}",
            )
        self.assertEqual(
            len(runtime.registry),
            len(COMMERCE_WRITE_TOOLS) + len(SEO_WRITE_TOOLS) + len(B2B_WRITE_TOOLS),
        )

    def test_ah_enabled_valid_composes_adapter(self):
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env={
                "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
                "GITHUB_WRITE_DRY_RUN": "true",
                "GITHUB_WRITE_KILL_SWITCH": "true",
            },
        )
        self.assertIsNotNone(runtime.registry.get(GITHUB_TOOL_ID))
        self.assertIsInstance(runtime.registry.get(GITHUB_TOOL_ID)._transport, GitHubHttpTransport)

    def test_ak_fake_not_production_default(self):
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_test"}),
            env={
                "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
            },
        )
        transport = runtime.registry.get(GITHUB_TOOL_ID)._transport
        self.assertIsInstance(transport, GitHubHttpTransport)
        self.assertNotIsInstance(transport, FakeGitHubTransport)

    def test_ai_aj_analyze_isolated_from_github_errors(self):
        main_mod = load_app(
            **env_for("openai"),
            GITHUB_WRITE_ADAPTER_ENABLED="true",
            GITHUB_ALLOWED_REPOSITORIES="octo/hello",
        )
        self.assertIsNotNone(main_mod.side_effect_runtime)
        self.assertIsNone(main_mod.side_effect_runtime.registry.get(GITHUB_TOOL_ID))
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

    def test_ba_analyze_seven_fields(self):
        self.test_ai_aj_analyze_isolated_from_github_errors()

    def test_bb_tool_gateway(self):
        self.assertEqual(ToolGateway().tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)

    def test_bi_mode_auto(self):
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

    def test_bj_mode_both(self):
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

    def test_at_health_no_token(self):
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets({"GITHUB_WRITE_TOKEN": "ghs_secret_value"}),
            env={
                "GITHUB_WRITE_ADAPTER_ENABLED": "true",
                "GITHUB_ALLOWED_REPOSITORIES": "octo/hello",
            },
        )
        blob = str(runtime.health()) + repr(runtime.config) + str(runtime.health().metadata)
        self.assertNotIn("ghs_secret_value", blob)
        self.assertNotIn("Authorization", blob)
