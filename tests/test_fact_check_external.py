from fastapi.testclient import TestClient
import unittest

from agents.fact_validator import FactValidator
from agents.validators.confidence import compute_confidence
from agents.validators.models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    ConfidenceInputs,
)
from tools.evidence import extract_claims
from tools.gateway import ToolGateway
from tools.models import MAX_FACT_CLAIMS, TRUST_UNKNOWN
from tools.search.fake_provider import FakeSearchProvider, fake_result
from tests.test_mode_routing import env_for, mock_provider_runs
from tests.test_smoke import CONTRACT_KEYS, load_app


CLAIM = "The WidgetIndex reached 12.5% in 2024."
PROMPT = "Найди поставщика KEEP_PROMPT_OUT_OF_SEARCH"


def _inputs(**overrides):
    values = dict(
        successful_experts=1,
        failed_providers=0,
        structural_fail=False,
        structural_all_pass=True,
        consistency_status=STATUS_UNKNOWN,
        sources_present=False,
        factual_status=STATUS_UNKNOWN,
        category="research",
    )
    values.update(overrides)
    return ConfidenceInputs(**values)


class FactCheckExternalTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_two_independent_trusted_sources_support(self):
        fake = FakeSearchProvider(
            {
                "WidgetIndex": [
                    fake_result(
                        "https://en.wikipedia.org/wiki/WidgetIndex",
                        title="WidgetIndex",
                        snippet="The WidgetIndex reached 12.5% in 2024.",
                    ),
                    fake_result(
                        "https://www.nih.gov/news/widgetindex",
                        title="NIH WidgetIndex",
                        snippet="WidgetIndex reached 12.5% in 2024.",
                    ),
                ]
            }
        )
        result = await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": CLAIM},
            category="research",
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.reason, "independent_supporting_sources")

    async def test_b_supporting_and_contradictory_evidence(self):
        fake = FakeSearchProvider(
            {
                "WidgetIndex": [
                    fake_result(
                        "https://en.wikipedia.org/wiki/WidgetIndex",
                        snippet="WidgetIndex increased 12.5% in 2024.",
                    ),
                    fake_result(
                        "https://www.nih.gov/news/widgetindex",
                        snippet="WidgetIndex decreased during the period.",
                    ),
                ]
            }
        )
        result = await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": "WidgetIndex increased 12.5% in 2024."},
            category="research",
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertEqual(result.reason, "contradicting_evidence")

    async def test_c_low_trust_only_is_insufficient(self):
        fake = FakeSearchProvider(
            {
                "WidgetIndex": [
                    fake_result(
                        "https://blog.example.com/widgetindex",
                        snippet="The WidgetIndex reached 12.5% in 2024.",
                        trust_level=TRUST_UNKNOWN,
                    )
                ]
            }
        )
        result = await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": CLAIM},
            category="research",
        )
        self.assertIn(result.status, {STATUS_UNKNOWN})
        self.assertEqual(result.reason, "low_trust_only")
        self.assertNotEqual(result.status, STATUS_PASS)

    async def test_d_same_domain_is_not_independent(self):
        fake = FakeSearchProvider(
            {
                "WidgetIndex": [
                    fake_result(
                        "https://en.wikipedia.org/wiki/WidgetIndex",
                        snippet="WidgetIndex reached 12.5% in 2024.",
                    ),
                    fake_result(
                        "https://en.wikipedia.org/wiki/WidgetIndex_2024",
                        snippet="WidgetIndex reached 12.5% in 2024.",
                    ),
                ]
            }
        )
        result = await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": CLAIM},
            category="research",
        )
        self.assertNotEqual(result.status, STATUS_PASS)
        self.assertEqual(result.reason, "single_source_insufficient")

    async def test_e_timeout_is_unknown_and_http_stays_up(self):
        fake = FakeSearchProvider({"WidgetIndex": []})
        fake.delay_seconds = 1
        result = await FactValidator(
            gateway=ToolGateway(fake, timeout_seconds=0.01)
        ).validate(
            {"openai": CLAIM},
            category="research",
        )
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.reason, "external_evidence_timeout")

        http_fake = FakeSearchProvider({"WidgetIndex": []})
        http_fake.delay_seconds = 1
        main_mod = load_app(**env_for("openai"))
        main_mod.router.pipeline.fact_validator.gateway = ToolGateway(
            http_fake,
            timeout_seconds=0.01,
        )
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        mocks["openai"].return_value = CLAIM
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai", "role": "researcher"},
            )
        self.assertEqual(response.status_code, 200)

    async def test_f_search_error_is_unknown_not_http_500(self):
        fake = FakeSearchProvider()
        fake.error = RuntimeError("search backend down")
        result = await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": CLAIM},
            category="research",
        )
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.reason, "external_evidence_unavailable")

        main_mod = load_app(**env_for("openai"))
        failing = FakeSearchProvider()
        failing.error = RuntimeError("search backend down")
        main_mod.router.pipeline.fact_validator.gateway = ToolGateway(failing)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        mocks["openai"].return_value = CLAIM
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai", "role": "researcher"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.status_code, 500)

    async def test_m_query_does_not_contain_secret(self):
        secret = "sk-search-secret-value"
        fake = FakeSearchProvider()
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"SEARCH_API_KEY": secret}, clear=False):
            await FactValidator(gateway=ToolGateway(fake)).validate(
                {"openai": f"GDP grew 3.2% using {secret}"},
                category="research",
            )
        dumped = " ".join(fake.queries)
        self.assertNotIn(secret, dumped)

    async def test_n_full_prompt_is_not_sent_to_search(self):
        fake = FakeSearchProvider(
            {
                "WidgetIndex": [
                    fake_result(
                        "https://en.wikipedia.org/wiki/WidgetIndex",
                        snippet=CLAIM,
                    )
                ]
            }
        )
        main_mod = load_app(**env_for("openai"))
        main_mod.router.pipeline.fact_validator.gateway = ToolGateway(fake)
        manager = main_mod.router.pipeline.expert_manager
        stack, mocks = mock_provider_runs(manager, "openai")
        mocks["openai"].return_value = CLAIM
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai", "role": "researcher"},
            )
        self.assertEqual(response.status_code, 200)
        dumped = " ".join(fake.queries)
        self.assertNotIn("KEEP_PROMPT_OUT_OF_SEARCH", dumped)

    async def test_o_max_fact_claims_enforced(self):
        answers = " ".join(f"Metric{index} grew {index}.5% in 202{index}." for index in range(8))
        claims = extract_claims([answers], limit=MAX_FACT_CLAIMS)
        self.assertLessEqual(len(claims), MAX_FACT_CLAIMS)
        fake = FakeSearchProvider()
        await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": answers},
            category="research",
        )
        self.assertLessEqual(len(fake.queries), MAX_FACT_CLAIMS)

    async def test_q_strategy_does_not_call_search(self):
        fake = FakeSearchProvider()
        result = await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": CLAIM},
            category="strategy",
        )
        self.assertEqual(fake.queries, [])
        self.assertEqual(result.reason, "no_external_evidence")

    async def test_r_research_and_trend_are_eligible(self):
        fake = FakeSearchProvider()
        await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": CLAIM},
            category="research",
        )
        await FactValidator(gateway=ToolGateway(fake)).validate(
            {"openai": CLAIM},
            category="trend_analysis",
        )
        self.assertGreaterEqual(len(fake.queries), 2)

    def test_s_supported_confidence_higher_than_unknown(self):
        unknown = compute_confidence(_inputs(factual_status=STATUS_UNKNOWN))
        supported = compute_confidence(_inputs(factual_status=STATUS_PASS))
        self.assertGreater(supported, unknown)

    def test_t_contradicted_confidence_lower_than_unknown(self):
        unknown = compute_confidence(_inputs(factual_status=STATUS_UNKNOWN))
        contradicted = compute_confidence(_inputs(factual_status=STATUS_FAIL))
        self.assertLess(contradicted, unknown)

    def test_v_success_contract_unchanged(self):
        main_mod = load_app(**env_for("openai"))
        manager = main_mod.router.pipeline.expert_manager
        stack, _ = mock_provider_runs(manager, "openai")
        with stack:
            client = TestClient(main_mod.app)
            response = client.post(
                "/api/analyze",
                json={"prompt": PROMPT, "mode": "openai"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload.keys()), set(CONTRACT_KEYS))
        self.assertEqual(payload["role"], "Judge")
        self.assertNotIn("sources", payload)
        self.assertNotIn("evidence", payload)


if __name__ == "__main__":
    unittest.main()
