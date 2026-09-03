"""Blocks 22–36 operational activation — offline/honest evidence tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from operational_activation.channels import (
    evaluate_channel_access,
    telegram_live_boundary,
    telegram_token_status,
    voice_live_boundary,
)
from operational_activation.hitl_write import HitlWriteGovernor, fingerprint_params
from operational_activation.metrics_honesty import (
    bottleneck_status,
    load_harness_safety,
    metrics_instrumentation_status,
    optimization_status,
    percentile_dashboard_capability,
)
from operational_activation.product_definition import product_definition
from operational_activation.registry import block_status_report
from operational_activation.status import (
    HUMAN_APPROVAL_REQUIRED,
    METRICS_INSTRUMENTATION_READY,
    REAL_PRODUCTION_SAMPLE_INSUFFICIENT,
    WAITING_FOR_EVIDENCE,
    WRITE_APPROVAL_REQUIRED,
    WRITE_FAILED,
)
from scale_optimization.metrics import aggregate_percentiles


class ProductDefinitionTests(unittest.TestCase):
    def test_no_hype_and_marketplace_not_live(self):
        d = product_definition()
        self.assertEqual(d["name"], "Panda")
        claims = {c["id"]: c for c in d["capabilities"]}
        self.assertEqual(claims["marketplace"]["availability"], "NOT_ACTIVATED")
        self.assertNotIn("revolutionary", d["what_is"].lower())
        for bad in d["unsupported_marketing_claims"]:
            self.assertNotIn(bad.lower(), d["what_is"].lower())


class HitlWriteGovernorTests(unittest.TestCase):
    def test_fingerprint_and_external_write_disabled(self):
        gov = HitlWriteGovernor(default_ttl_seconds=3600)
        prop = gov.propose(
            tenant_id="t1",
            actor_id="t1:u1",
            action="MARKETPLACE_PRICE_UPDATE",
            resource="sku-1",
            params={"price": 100, "sku": "A"},
            integration="wb",
            idempotency_key="k1",
        )
        self.assertEqual(prop.state, WRITE_APPROVAL_REQUIRED)
        fp = fingerprint_params({"price": 100, "sku": "A"})
        gov.approve(proposal_id=prop.proposal_id, approver_id="owner", tenant_id="t1", expected_fingerprint=fp)
        done = gov.execute(proposal_id=prop.proposal_id, tenant_id="t1")
        self.assertEqual(done.state, WRITE_FAILED)
        self.assertEqual(done.result, "REAL_EXTERNAL_WRITE_DISABLED")
        self.assertEqual(gov.real_external_writes, 0)

    def test_material_parameter_mutation_denied(self):
        gov = HitlWriteGovernor()
        prop = gov.propose(
            tenant_id="t1",
            actor_id="t1:u1",
            action="SAFE_INTERNAL_WRITE",
            resource="doc",
            params={"qty": 1},
            integration="internal",
            idempotency_key="k2",
        )
        gov.approve(proposal_id=prop.proposal_id, approver_id="o", tenant_id="t1")
        mutated = gov.execute(proposal_id=prop.proposal_id, tenant_id="t1", params_now={"qty": 99})
        self.assertEqual(mutated.result, "material_parameter_mutation")

    def test_cross_tenant_approval_denied(self):
        gov = HitlWriteGovernor()
        prop = gov.propose(
            tenant_id="t1",
            actor_id="t1:u1",
            action="READ",
            resource="x",
            params={},
            integration="internal",
            idempotency_key="k3",
        )
        with self.assertRaises(PermissionError):
            gov.approve(proposal_id=prop.proposal_id, approver_id="o", tenant_id="t2")

    def test_idempotent_propose(self):
        gov = HitlWriteGovernor()
        a = gov.propose(
            tenant_id="t1",
            actor_id="a",
            action="READ",
            resource="r",
            params={},
            integration="i",
            idempotency_key="same",
        )
        b = gov.propose(
            tenant_id="t1",
            actor_id="a",
            action="READ",
            resource="r",
            params={},
            integration="i",
            idempotency_key="same",
        )
        self.assertEqual(a.proposal_id, b.proposal_id)


class MetricsHonestyTests(unittest.TestCase):
    def test_no_fabricated_production_sample(self):
        m = metrics_instrumentation_status(real_sample_count=0)
        self.assertEqual(m["status"], METRICS_INSTRUMENTATION_READY)
        self.assertEqual(m["detail"], REAL_PRODUCTION_SAMPLE_INSUFFICIENT)
        self.assertFalse(m["real_sample"])

    def test_percentile_fixture_math(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        stats = aggregate_percentiles(values)
        dash = percentile_dashboard_capability(values=values)
        self.assertEqual(dash["fixture_math"]["p50"], stats.p50)
        self.assertEqual(dash["production_claim"], "NOT_CLAIMED")
        self.assertFalse(dash["real_sample"])

    def test_bottleneck_none_without_evidence(self):
        self.assertEqual(bottleneck_status()["bottleneck"], "NONE")
        self.assertEqual(optimization_status()["status"], WAITING_FOR_EVIDENCE)
        self.assertFalse(load_harness_safety()["production_load_executed"])


class ChannelBoundaryTests(unittest.TestCase):
    def test_token_missing_only(self):
        self.assertEqual(telegram_token_status({}), "MISSING")
        self.assertEqual(telegram_token_status({"TELEGRAM_BOT_TOKEN": "x"}), "PRESENT")
        b = telegram_live_boundary({})
        self.assertEqual(b["status"], HUMAN_APPROVAL_REQUIRED)
        self.assertTrue(b["network_request"])
        vb = voice_live_boundary({})
        self.assertEqual(vb["status"], HUMAN_APPROVAL_REQUIRED)


class RegistryAndPublicRoutesTests(unittest.TestCase):
    def test_block_report_truthful(self):
        report = block_status_report()
        self.assertEqual(report["26_hitl_write"]["real_write_count"], 0)
        self.assertFalse(report["27_production_metrics"]["real_sample"])
        self.assertFalse(report["28_p95_p99"]["real_sample"])
        self.assertFalse(report["29_real_load"]["production_load_executed"])
        self.assertEqual(report["30_proven_bottleneck"]["bottleneck"], "NONE")
        self.assertEqual(report["32_limited_pilot"]["real_users_count"], 0)
        self.assertFalse(report["34_expansion"]["expanded_users"])
        self.assertFalse(report["35_scaling"]["infra_mutation"])
        self.assertTrue(report["36_public_website"]["legal_status"]["pre_publication_blocker"])

    def test_public_html_files_exist(self):
        root = Path("static/public")
        for name in (
            "index.html",
            "product.html",
            "capabilities.html",
            "use-cases.html",
            "plans.html",
            "faq.html",
            "security.html",
            "contact.html",
            "register.html",
            "public.css",
            "site.js",
        ):
            self.assertTrue((root / name).is_file(), name)


class OperationalHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["SECURITY_AUTH_MODE"] = "required"
        os.environ["PANDA_API_KEYS"] = "key-a|tenant-a|user-a|user|secret-a"
        os.environ["TELEGRAM_INTERFACE_ENABLED"] = "false"
        os.environ["VOICE_INTERFACE_ENABLED"] = "false"
        os.environ["SAAS_PRODUCT_DB_PATH"] = os.path.join(cls.tmp, "saas.sqlite")
        os.environ["ACCOUNTS_DB_PATH"] = os.path.join(cls.tmp, "accounts.sqlite")
        import importlib
        import main as main_mod
        from security.api_auth import AuthService, configure_security

        configure_security(auth=AuthService(env={"SECURITY_AUTH_MODE": "required", "PANDA_API_KEYS": os.environ["PANDA_API_KEYS"]}))
        importlib.reload(main_mod)
        cls.client = TestClient(main_mod.app)

    def test_status_and_product_definition(self):
        r = self.client.get("/api/v1/operational/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("22_telegram", body)
        r2 = self.client.get("/api/v1/operational/product-definition")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["name"], "Panda")

    def test_public_routes_and_seo(self):
        for path in ("/", "/product", "/capabilities", "/use-cases", "/plans", "/faq", "/security", "/contact", "/register", "/app"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("Panda", r.text)
        home = self.client.get("/")
        self.assertIn('name="description"', home.text)
        self.assertIn("rel=\"canonical\"", home.text)
        self.assertIn("управляемый бизнес-ассистент", home.text.lower())
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertIn("Sitemap:", robots.text)
        sm = self.client.get("/sitemap.xml")
        self.assertEqual(sm.status_code, 200)
        self.assertIn("/capabilities", sm.text)

    def test_app_is_chat_not_marketing(self):
        r = self.client.get("/app")
        self.assertIn("/static/panda/js/app.js", r.text)

    def test_hitl_write_http_external_disabled(self):
        headers = {"X-API-Key": "secret-a", "X-Tenant-Id": "tenant-a"}
        prop = self.client.post(
            "/api/v1/operational/hitl-write/propose",
            headers=headers,
            json={
                "action": "EMAIL_SEND",
                "resource": "inbox",
                "params": {"to": "a@b.c", "body": "x"},
                "integration": "email",
                "idempotency_key": "http-write-1",
            },
        )
        self.assertEqual(prop.status_code, 200)
        pid = prop.json()["proposal_id"]
        fp = prop.json()["params_fingerprint"]
        apr = self.client.post(
            "/api/v1/operational/hitl-write/approve",
            headers=headers,
            json={"proposal_id": pid, "expected_fingerprint": fp},
        )
        self.assertEqual(apr.status_code, 200)
        exe = self.client.post(
            "/api/v1/operational/hitl-write/execute",
            headers=headers,
            json={"proposal_id": pid},
        )
        self.assertEqual(exe.status_code, 200)
        self.assertEqual(exe.json()["result"], "REAL_EXTERNAL_WRITE_DISABLED")

    def test_live_boundaries(self):
        tg = self.client.get("/api/v1/operational/telegram/live-boundary")
        self.assertEqual(tg.status_code, 200)
        self.assertEqual(tg.json()["status"], HUMAN_APPROVAL_REQUIRED)
        vo = self.client.get("/api/v1/operational/voice/live-boundary")
        self.assertEqual(vo.json()["status"], HUMAN_APPROVAL_REQUIRED)


class ChannelAccessUnitTests(unittest.TestCase):
    def test_missing_user_denied(self):
        class FakeStore:
            def get_user(self, _uid):
                return None

        class FakeSvc:
            store = FakeStore()
            access = None

        out = evaluate_channel_access(accounts_service=FakeSvc(), user_id=None, tenant_id="t", channel="telegram")
        self.assertFalse(out["allowed"])


if __name__ == "__main__":
    unittest.main()
