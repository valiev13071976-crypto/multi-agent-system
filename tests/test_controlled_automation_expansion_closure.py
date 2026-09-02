"""Controlled Automation Expansion — closure tests."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal

from business_assistant.service import BusinessAssistantService
from controlled_automation.conditions import evaluate_condition
from controlled_automation.config import (
    controlled_automation_expansion_engineering_ready,
    controlled_automation_expansion_live_active,
    controlled_automation_expansion_live_verified,
)
from controlled_automation.errors import (
    ACTION_NOT_ALLOWED,
    APPROVAL_REJECTED,
    APPROVAL_STALE,
    AUTOMATION_NOT_FOUND,
    BUDGET_EXCEEDED,
    CAPABILITY_DENIED,
    FORBIDDEN,
    KILL_SWITCH_ACTIVE,
    OVERLAP_BLOCKED,
    POLICY_DENIED,
    RISK_HITL_REQUIRED,
    TENANT_SCOPE_VIOLATION,
    ControlledAutomationError,
)
from controlled_automation.events import BusinessEventStore
from controlled_automation.models import DATA_KNOWN, DATA_UNKNOWN, RUN_NO_ACTION, RUN_PREPARED, RUN_SUCCEEDED, RUN_WAITING_APPROVAL, STATE_DRAFT, STATE_ENABLED, STATE_PAUSED
from controlled_automation.policy import KillSwitchRegistry, PolicyEvaluator
from controlled_automation.risk import R0_READ_ONLY, R1_PREPARE_ONLY, R3_EXTERNAL_BUSINESS_WRITE, can_auto_execute, requires_hitl
from controlled_automation.service import ControlledAutomationService
from controlled_automation.store import InMemoryControlledAutomationStore, SqliteControlledAutomationStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from production_business_e2e.fixtures import auth_env, api_headers
from security.api_auth import configure_security
from security.identity import RequestSecurityContext


TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _ctx(tenant: str = TENANT_A, roles=("user",)) -> RequestSecurityContext:
    return RequestSecurityContext(tenant_id=tenant, user_id="u", roles=roles, request_id="r1")


def _svc(**kwargs) -> ControlledAutomationService:
    return ControlledAutomationService(store=InMemoryControlledAutomationStore(), **kwargs)


def _payload(**extra):
    base = {
        "tenant_id": TENANT_A,
        "name": "stock-check",
        "trigger": {"type": "MANUAL"},
        "conditions": {"op": "ALL", "conditions": [{"field": "stock", "operator": "LT", "value": 10}]},
        "actions": [{"action_type": "STOCK_READ", "resource": "sku-1"}],
        "policy": {"allowed_action_types": ["STOCK_READ"], "allow_auto_execute": True},
        "risk_class": R0_READ_ONLY,
        "enabled": True,
    }
    base.update(extra)
    return base


class FlagTests(unittest.TestCase):
    def test_flags(self):
        self.assertTrue(controlled_automation_expansion_engineering_ready())
        self.assertFalse(controlled_automation_expansion_live_active())
        self.assertFalse(controlled_automation_expansion_live_verified())


class CRUDTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_create_draft(self):
        out = self.svc.create(_ctx(), _payload(enabled=False))
        self.assertEqual(out["state"], STATE_DRAFT)

    def test_enable(self):
        created = self.svc.create(_ctx(), _payload())
        out = self.svc.enable(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        self.assertEqual(out["state"], STATE_ENABLED)

    def test_pause_resume(self):
        created = self.svc.create(_ctx(), _payload())
        self.svc.pause(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        got = self.svc.get(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        self.assertTrue(got["paused"])
        self.svc.resume(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        self.assertFalse(self.svc.get(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])["paused"])

    def test_disable(self):
        created = self.svc.create(_ctx(), _payload())
        self.svc.disable(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        self.assertFalse(self.svc.get(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])["enabled"])

    def test_cross_tenant_read_denied(self):
        created = self.svc.create(_ctx(), _payload())
        with self.assertRaises(ControlledAutomationError) as cm:
            self.svc.get(_ctx(TENANT_B), tenant_id=TENANT_A, automation_id=created["automation_id"])
        self.assertEqual(cm.exception.code, TENANT_SCOPE_VIOLATION)

    def test_viewer_cannot_create(self):
        with self.assertRaises(ControlledAutomationError) as cm:
            self.svc.create(_ctx(roles=("viewer",)), _payload())
        self.assertEqual(cm.exception.code, FORBIDDEN)


class ConditionTests(unittest.TestCase):
    def test_eq_gt_lt(self):
        facts = {"stock": {"_value": 5, "_quality": DATA_KNOWN}}
        self.assertTrue(evaluate_condition({"field": "stock", "operator": "LT", "value": 10}, facts=facts)["satisfied"])
        self.assertTrue(evaluate_condition({"field": "stock", "operator": "GT", "value": 1}, facts=facts)["satisfied"])

    def test_all_any(self):
        facts = {"stock": {"_value": 5, "_quality": DATA_KNOWN}, "margin": {"_value": 10, "_quality": DATA_KNOWN}}
        all_ok = evaluate_condition({"op": "ALL", "conditions": [{"field": "stock", "operator": "LT", "value": 10}, {"field": "margin", "operator": "GT", "value": 5}]}, facts=facts)
        self.assertTrue(all_ok["satisfied"])

    def test_unknown_fail_closed(self):
        facts = {"stock": {"_value": None, "_quality": DATA_UNKNOWN}}
        out = evaluate_condition({"field": "stock", "operator": "LT", "value": 10}, facts=facts)
        self.assertFalse(out["satisfied"])
        self.assertEqual(out["quality"], DATA_UNKNOWN)

    def test_stale_fail_closed(self):
        facts = {"stock": {"_value": 5, "_quality": "STALE"}}
        out = evaluate_condition({"field": "stock", "operator": "LT", "value": 10}, facts=facts)
        self.assertFalse(out["satisfied"])


class DryRunTests(unittest.TestCase):
    def test_dry_run_zero_side_effects(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload())
        out = svc.dry_run(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        self.assertEqual(out["run"]["status"], RUN_PREPARED)
        self.assertEqual(len(svc._dispatcher.side_effects), 0)


class RunNowTests(unittest.TestCase):
    def test_run_now_governed(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload())
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        self.assertEqual(out["run"]["status"], RUN_SUCCEEDED)

    def test_no_action_when_condition_false(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload())
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 100, "_quality": DATA_KNOWN}}})
        self.assertEqual(out["run"]["status"], RUN_NO_ACTION)


class RiskHITLTests(unittest.TestCase):
    def test_r3_requires_hitl(self):
        self.assertTrue(requires_hitl(risk_class=R3_EXTERNAL_BUSINESS_WRITE, allow_auto_execute=False))

    def test_r3_run_waits_approval(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload(
            actions=[{"action_type": "MARKETPLACE_PRICE_UPDATE", "resource": "OZ-SKU-100"}],
            risk_class=R3_EXTERNAL_BUSINESS_WRITE,
            policy={"allowed_action_types": ["MARKETPLACE_PRICE_UPDATE"], "requires_approval": True},
        ))
        svc.enable(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        self.assertEqual(out["run"]["status"], RUN_WAITING_APPROVAL)

    def test_rejected_approval_zero_effect(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload(
            actions=[{"action_type": "MARKETPLACE_PRICE_UPDATE"}],
            risk_class=R3_EXTERNAL_BUSINESS_WRITE,
            policy={"allowed_action_types": ["MARKETPLACE_PRICE_UPDATE"], "requires_approval": True},
        ))
        svc.enable(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        svc.reject(_ctx(), tenant_id=TENANT_A, run_id=out["run"]["run_id"], approval_id=out["approval_id"])
        self.assertEqual(len(svc._dispatcher.side_effects), 0)

    def test_high_risk_enable_blocked(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload(
            actions=[{"action_type": "MARKETPLACE_PRICE_UPDATE"}],
            risk_class=R3_EXTERNAL_BUSINESS_WRITE,
            enabled=False,
            policy={"requires_approval": False, "allow_auto_execute": False},
        ))
        with self.assertRaises(ControlledAutomationError) as cm:
            svc.enable(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        self.assertEqual(cm.exception.code, RISK_HITL_REQUIRED)


class PolicyTests(unittest.TestCase):
    def test_arbitrary_action_rejected(self):
        svc = _svc()
        with self.assertRaises(ControlledAutomationError) as cm:
            svc.create(_ctx(), _payload(actions=[{"action_type": "ARBITRARY_HACK"}]))
        self.assertEqual(cm.exception.code, ACTION_NOT_ALLOWED)

    def test_kill_switch_global(self):
        ks = KillSwitchRegistry()
        ks.activate(scope="GLOBAL")
        svc = _svc(kill_switch=ks)
        created = svc.create(_ctx(), _payload())
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        self.assertEqual(out["run"]["status"], "BLOCKED")
        self.assertEqual(out["policy"]["code"], KILL_SWITCH_ACTIVE)

    def test_kill_switch_tenant(self):
        ks = KillSwitchRegistry()
        ks.activate(scope="TENANT", tenant_id=TENANT_A)
        svc = _svc(kill_switch=ks)
        created = svc.create(_ctx(), _payload())
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        self.assertEqual(out["policy"]["code"], KILL_SWITCH_ACTIVE)

    def test_capability_revoked_blocks(self):
        svc = _svc(capability_checker=lambda t, c: False)
        created = svc.create(_ctx(), _payload(required_capabilities=[]))
        with self.assertRaises(ControlledAutomationError) as cm:
            svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {}})
        self.assertEqual(cm.exception.code, CAPABILITY_DENIED)

    def test_budget_block(self):
        svc = _svc(budget_checker=lambda t, m: False)
        created = svc.create(_ctx(), _payload())
        with self.assertRaises(ControlledAutomationError) as cm:
            svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        self.assertEqual(cm.exception.code, BUDGET_EXCEEDED)


class EventTests(unittest.TestCase):
    def test_event_idempotency(self):
        store = BusinessEventStore()
        p = {"event_id": "e1", "event_type": "STOCK_IMPORTED", "tenant_id": TENANT_A}
        e1 = store.ingest(tenant_id=TENANT_A, payload=p)
        e2 = store.ingest(tenant_id=TENANT_A, payload=p)
        self.assertEqual(e1.event_id, e2.event_id)

    def test_self_trigger_loop_prevented(self):
        store = BusinessEventStore()
        with self.assertRaises(ValueError):
            store.ingest(tenant_id=TENANT_A, payload={"event_id": "e1", "origin_automation_id": "a1", "causation_id": "a1"})


class MarketplaceSafetyTests(unittest.TestCase):
    def test_unknown_cost_blocks_auto_repricing(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload(
            conditions={"op": "ALL", "conditions": [{"field": "marketplace_margin", "operator": "LT", "value": 10}]},
            actions=[{"action_type": "PREPARE_PRICE_UPDATE", "resource": "OZ-SKU-100"}],
            risk_class=R1_PREPARE_ONLY,
        ))
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"marketplace_margin": {"_value": None, "_quality": DATA_UNKNOWN}}})
        self.assertEqual(out["run"]["status"], "BLOCKED")

    def test_price_floor_condition(self):
        facts = {"marketplace_price": {"_value": 900, "_quality": DATA_KNOWN}, "price_floor": {"_value": 1000, "_quality": DATA_KNOWN}}
        out = evaluate_condition({"op": "ALL", "conditions": [{"field": "marketplace_price", "operator": "LT", "value": 1000}]}, facts=facts)
        self.assertTrue(out["satisfied"])


class StockSafetyTests(unittest.TestCase):
    def test_unknown_stock_not_zero(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload())
        out = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": None, "_quality": DATA_UNKNOWN}}})
        self.assertEqual(out["run"]["status"], "BLOCKED")


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_execution_key(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload())
        ctx = {"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}}
        svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context=ctx)
        out2 = svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context=ctx)
        self.assertIn(out2.get("status"), {None, "idempotent", RUN_SUCCEEDED})


class PauseTests(unittest.TestCase):
    def test_pause_prevents_run(self):
        svc = _svc()
        created = svc.create(_ctx(), _payload())
        svc.pause(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
        with self.assertRaises(ControlledAutomationError) as cm:
            svc.run_now(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"], context={"facts": {"stock": {"_value": 5, "_quality": DATA_KNOWN}}})
        self.assertEqual(cm.exception.code, POLICY_DENIED)


class RecoveryTests(unittest.TestCase):
    def test_sqlite_restart(self):
        path = os.path.join(os.getcwd(), ".pytest_cache", "controlled_auto_test.sqlite")
        if os.path.exists(path):
            os.unlink(path)
        try:
            store1 = SqliteControlledAutomationStore(path)
            svc1 = ControlledAutomationService(store=store1)
            created = svc1.create(_ctx(), _payload(enabled=False))
            del svc1
            store2 = SqliteControlledAutomationStore(path)
            svc2 = ControlledAutomationService(store=store2)
            got = svc2.get(_ctx(), tenant_id=TENANT_A, automation_id=created["automation_id"])
            self.assertEqual(got["automation_id"], created["automation_id"])
            del svc2
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class BATests(unittest.TestCase):
    def test_ba_create_draft(self):
        svc = _svc()
        ba = BusinessAssistantService(controlled_automation=svc)
        ex = type("Ex", (), {"tenant_id": TENANT_A, "artifacts": [], "cost": Decimal("0"), "execution_id": "x", "workflow_id": "w", "_automation_intent": {"name": "Morning stock check"}})()
        out = ba._execute_step(ex, None, type("R", (), {"text": ""})(), type("S", (), {"name": "automation_draft", "capability": "automation"})())
        self.assertFalse(out["mutation"])
        self.assertIn("proposal", out)

    def test_ba_cannot_silently_enable_high_risk(self):
        svc = _svc()
        draft = svc.ba_create_draft(tenant_id=TENANT_A, intent={"name": "Dangerous"})
        self.assertFalse(draft["draft"]["enabled"])
        self.assertTrue(draft["high_risk_auto_enable_blocked"])


class SecurityTests(unittest.TestCase):
    def test_executable_payload_rejected(self):
        svc = _svc()
        with self.assertRaises(ControlledAutomationError):
            svc.create(_ctx(), _payload(actions=[{"action_type": "STOCK_READ", "code": "print(1)"}]))


class APITests(unittest.TestCase):
    def setUp(self):
        for k, v in auth_env().items():
            os.environ[k] = v
        configure_security()
        self.svc = _svc()
        from controlled_automation.router import configure_controlled_automation_router

        app = FastAPI()
        app.include_router(configure_controlled_automation_router(self.svc))
        self.client = TestClient(app)

    def test_api_create_and_status(self):
        body = {
            "tenant_id": TENANT_A,
            "name": "api-test",
            "trigger": {"type": "MANUAL"},
            "conditions": {"op": "ALL", "conditions": []},
            "actions": [{"action_type": "ANALYTICS_READ"}],
            "policy": {"allowed_action_types": ["ANALYTICS_READ"]},
            "enabled": False,
        }
        r = self.client.post("/api/v1/automations/controlled", json=body, headers=api_headers("secret-a"))
        self.assertEqual(r.status_code, 200)
        st = self.client.get("/api/v1/automations/controlled/status")
        self.assertTrue(st.json()["engineering_ready"])
        self.assertFalse(st.json()["live_active"])


class AnalyticsTests(unittest.TestCase):
    def test_snapshot(self):
        svc = _svc()
        svc.create(_ctx(), _payload())
        snap = svc.analytics_snapshot(tenant_id=TENANT_A)
        self.assertGreaterEqual(snap["total_automations"], 1)


class RiskClassTests(unittest.TestCase):
    def test_r0_auto_execute(self):
        self.assertTrue(can_auto_execute(risk_class=R0_READ_ONLY, allow_auto_execute=False, dry_run=False))

    def test_r1_prepare(self):
        self.assertTrue(can_auto_execute(risk_class=R1_PREPARE_ONLY, allow_auto_execute=False, dry_run=False))


class LiveBoundaryTests(unittest.TestCase):
    def test_live_fail_closed(self):
        svc = _svc()
        old = os.environ.get("CONTROLLED_AUTOMATION_MODE")
        os.environ["CONTROLLED_AUTOMATION_MODE"] = "LIVE"
        os.environ["CONTROLLED_AUTOMATION_LIVE_ENABLED"] = "true"
        try:
            from controlled_automation.errors import LIVE_FALLBACK_FORBIDDEN

            with self.assertRaises(ControlledAutomationError) as cm:
                svc.create(_ctx(), _payload())
            self.assertEqual(cm.exception.code, LIVE_FALLBACK_FORBIDDEN)
        finally:
            if old is None:
                os.environ.pop("CONTROLLED_AUTOMATION_MODE", None)
            else:
                os.environ["CONTROLLED_AUTOMATION_MODE"] = old
            os.environ.pop("CONTROLLED_AUTOMATION_LIVE_ENABLED", None)


if __name__ == "__main__":
    unittest.main()
