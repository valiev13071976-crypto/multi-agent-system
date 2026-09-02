"""Scheduled Business Automation — closure tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from business_assistant.service import BusinessAssistantService
from scheduled_automation.access import PERM_SCHEDULE_CREATE
from scheduled_automation.clock import Clock
from scheduled_automation.config import (
    MIN_INTERVAL_SECONDS,
    scheduled_business_automation_engineering_ready,
    scheduled_business_automation_live_active,
    scheduled_business_automation_live_verified,
)
from scheduled_automation.dispatcher import ScheduledAutomationDispatcher
from scheduled_automation.errors import (
    CAPABILITY_DENIED,
    FORBIDDEN,
    INVALID_RECURRENCE,
    INVALID_SCHEDULE,
    INVALID_TIMEZONE,
    LIVE_FALLBACK_FORBIDDEN,
    STALE_VERSION,
    TENANT_SCOPE_VIOLATION,
    UNSUPPORTED_TARGET,
    ScheduledAutomationError,
)
from scheduled_automation.models import (
    MISFIRE_RUN_ONCE,
    MISFIRE_SKIP,
    OCC_DISPATCHED,
    OCC_SKIPPED,
    OCC_WAITING_APPROVAL,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    SCHEDULE_WEEKLY,
    TARGET_ANALYTICS,
    ScheduleDefinition,
)
from scheduled_automation.recurrence import compute_next_run, execution_key, occurrence_id, parse_utc, validate_timezone
from scheduled_automation.router import configure_scheduled_automation_router
from scheduled_automation.service import ScheduledAutomationService
from scheduled_automation.store import InMemoryScheduleAutomationStore, SqliteScheduleAutomationStore
from security.api_auth import configure_security
from security.identity import RequestSecurityContext


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user|secret-a;"
            "key-b|tenant-b|user-b|user|secret-b;"
            "key-view|tenant-a|viewer|viewer|secret-view"
        ),
        "SCHEDULED_AUTOMATION_MODE": "FIXTURE",
    }


def _headers(key: str) -> dict:
    return {"X-API-Key": key}


def _ctx(tenant: str = "tenant-a", roles=("user",), user: str = "u1") -> RequestSecurityContext:
    return RequestSecurityContext(tenant_id=tenant, user_id=user, roles=roles, request_id="r1")


def _base_payload(**extra):
    base = {
        "tenant_id": "tenant-a",
        "name": "test",
        "schedule_type": SCHEDULE_INTERVAL,
        "timezone": "UTC",
        "start_at": "2026-01-01T00:00:00+00:00",
        "interval_seconds": 300,
        "target_type": TARGET_ANALYTICS,
        "target_payload": {"question_type": "sales_week"},
    }
    base.update(extra)
    return base


def _schedule_from_created(created: dict, **overrides) -> ScheduleDefinition:
    fields = ScheduleDefinition.__dataclass_fields__
    data = {k: v for k, v in created.items() if k in fields}
    data.update(overrides)
    return ScheduleDefinition(**data)

def _svc(
    *,
    now: datetime | None = None,
    caps: set[str] | None = None,
    budget_ok: bool = True,
    dispatch_fn=None,
) -> ScheduledAutomationService:
    clock = Clock(now=now or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    granted = caps if caps is not None else {"schedule.create", "analytics.read"}

    def cap_checker(tenant: str, required: tuple[str, ...]) -> bool:
        return all(c in granted for c in required)

    def budget_checker(tenant: str, meta: dict) -> bool:
        return budget_ok

    dispatcher = ScheduledAutomationDispatcher(dispatch_fn=dispatch_fn)
    return ScheduledAutomationService(
        store=InMemoryScheduleAutomationStore(),
        dispatcher=dispatcher,
        clock=clock,
        capability_checker=cap_checker,
        budget_checker=budget_checker,
    )


class FlagTests(unittest.TestCase):
    def test_engineering_flags(self):
        self.assertTrue(scheduled_business_automation_engineering_ready())
        self.assertFalse(scheduled_business_automation_live_active())
        self.assertFalse(scheduled_business_automation_live_verified())


class RecurrenceTests(unittest.TestCase):
    def test_once_next_run(self):
        n = compute_next_run(
            schedule_type=SCHEDULE_ONCE,
            timezone_name="UTC",
            start_at="2026-02-01T10:00:00+00:00",
            end_at=None,
            interval_seconds=None,
            daily_time=None,
            weekly_day=None,
            from_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            occurrence_count=0,
            max_occurrences=None,
        )
        self.assertEqual(n, parse_utc("2026-02-01T10:00:00+00:00"))

    def test_interval_respects_minimum(self):
        with self.assertRaises(ScheduledAutomationError) as cm:
            compute_next_run(
                schedule_type=SCHEDULE_INTERVAL,
                timezone_name="UTC",
                start_at="2026-01-01T00:00:00+00:00",
                end_at=None,
                interval_seconds=5,
                daily_time=None,
                weekly_day=None,
                from_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                occurrence_count=0,
                max_occurrences=None,
            )
        self.assertEqual(cm.exception.code, INVALID_RECURRENCE)

    def test_daily_timezone(self):
        n = compute_next_run(
            schedule_type=SCHEDULE_DAILY,
            timezone_name="UTC",
            start_at="2026-01-01T00:00:00+00:00",
            end_at=None,
            interval_seconds=None,
            daily_time="09:00",
            weekly_day=None,
            from_time=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            occurrence_count=0,
            max_occurrences=None,
        )
        self.assertEqual(n.hour, 9)

    def test_weekly(self):
        n = compute_next_run(
            schedule_type=SCHEDULE_WEEKLY,
            timezone_name="UTC",
            start_at="2026-01-01T00:00:00+00:00",
            end_at=None,
            interval_seconds=None,
            daily_time="08:00",
            weekly_day=0,
            from_time=datetime(2026, 1, 7, 9, 0, tzinfo=timezone.utc),
            occurrence_count=0,
            max_occurrences=None,
        )
        self.assertEqual(n.weekday(), 0)

    def test_invalid_timezone(self):
        with self.assertRaises(ScheduledAutomationError) as cm:
            validate_timezone("Not/AZone")
        self.assertEqual(cm.exception.code, INVALID_TIMEZONE)

    def test_occurrence_identity(self):
        dt = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        oid = occurrence_id("s1", 2, dt)
        self.assertIn("s1:v2:", oid)
        self.assertIn("schedule-occurrence:s1:2:", execution_key("s1", 2, dt))


class ServiceCRUDTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()
        self.ctx = _ctx()

    def test_create_interval(self):
        out = self.svc.create_schedule(self.ctx, _base_payload())
        self.assertEqual(out["schedule_type"], SCHEDULE_INTERVAL)
        self.assertTrue(out["next_run_at"])

    def test_create_once(self):
        out = self.svc.create_schedule(self.ctx, _base_payload(schedule_type=SCHEDULE_ONCE, start_at="2026-02-01T10:00:00+00:00", interval_seconds=None))
        self.assertEqual(out["schedule_type"], SCHEDULE_ONCE)

    def test_create_daily(self):
        out = self.svc.create_schedule(self.ctx, _base_payload(schedule_type=SCHEDULE_DAILY, daily_time="09:00", timezone="UTC", interval_seconds=None))
        self.assertEqual(out["schedule_type"], SCHEDULE_DAILY)

    def test_minimum_frequency(self):
        with self.assertRaises(ScheduledAutomationError) as cm:
            self.svc.create_schedule(self.ctx, _base_payload(interval_seconds=10))
        self.assertEqual(cm.exception.code, INVALID_SCHEDULE)
        self.assertGreaterEqual(MIN_INTERVAL_SECONDS, 60)

    def test_unsupported_target(self):
        with self.assertRaises(ScheduledAutomationError) as cm:
            self.svc.create_schedule(self.ctx, _base_payload(target_type="ARBITRARY_CODE"))
        self.assertEqual(cm.exception.code, UNSUPPORTED_TARGET)

    def test_forbidden_payload_key(self):
        with self.assertRaises(ScheduledAutomationError):
            self.svc.create_schedule(self.ctx, _base_payload(target_payload={"code": "print(1)"}))

    def test_enable_disable_pause_resume(self):
        created = self.svc.create_schedule(self.ctx, _base_payload())
        sid = created["schedule_id"]
        self.svc.set_enabled(self.ctx, tenant_id="tenant-a", schedule_id=sid, enabled=False)
        got = self.svc.get_schedule(self.ctx, tenant_id="tenant-a", schedule_id=sid)
        self.assertFalse(got["enabled"])
        self.svc.pause(self.ctx, tenant_id="tenant-a", schedule_id=sid)
        got = self.svc.get_schedule(self.ctx, tenant_id="tenant-a", schedule_id=sid)
        self.assertTrue(got["paused"])
        self.svc.resume(self.ctx, tenant_id="tenant-a", schedule_id=sid)
        self.assertFalse(self.svc.get_schedule(self.ctx, tenant_id="tenant-a", schedule_id=sid)["paused"])

    def test_version_update(self):
        created = self.svc.create_schedule(self.ctx, _base_payload())
        sid = created["schedule_id"]
        updated = self.svc.update_schedule(self.ctx, tenant_id="tenant-a", schedule_id=sid, patch={"name": "renamed"}, expected_version=1)
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(ScheduledAutomationError) as cm:
            self.svc.update_schedule(self.ctx, tenant_id="tenant-a", schedule_id=sid, patch={"name": "x"}, expected_version=1)
        self.assertEqual(cm.exception.code, STALE_VERSION)

    def test_max_occurrences_disables(self):
        now = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        svc = _svc(now=now, dispatch_fn=lambda **k: {"run_id": "r1"})
        created = svc.create_schedule(_ctx(), _base_payload(schedule_type=SCHEDULE_ONCE, start_at=now.isoformat(), interval_seconds=None, max_occurrences=1))
        s = _schedule_from_created(created, next_run_at=now.isoformat())
        svc.store.update_schedule(s, expected_version=1)
        svc.tick(tenant_id="tenant-a")
        got = svc.get_schedule(_ctx(), tenant_id="tenant-a", schedule_id=created["schedule_id"])
        self.assertFalse(got["enabled"])


class TickDispatchTests(unittest.TestCase):
    def test_dispatch_on_due(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        dispatches = []

        def dispatch_fn(**kwargs):
            dispatches.append(kwargs)
            return {"run_id": "wf-1", "workflow_id": "wf-1"}

        svc = _svc(now=now, dispatch_fn=dispatch_fn)
        created = svc.create_schedule(_ctx(), _base_payload(start_at=(now - timedelta(hours=1)).isoformat()))
        s = _schedule_from_created(created, next_run_at=(now - timedelta(minutes=1)).isoformat())
        svc.store.update_schedule(s, expected_version=1)
        results = svc.tick(tenant_id="tenant-a")
        self.assertTrue(results)
        self.assertEqual(results[0]["status"], OCC_DISPATCHED)
        self.assertTrue(dispatches)
        self.assertEqual(dispatches[0]["execution_lane"], "scheduled")
        self.assertIn("schedule-occurrence:", dispatches[0]["execution_key"])

    def test_misfire_skip(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now)
        created = svc.create_schedule(_ctx(), _base_payload(misfire_policy=MISFIRE_SKIP, start_at=(now - timedelta(hours=2)).isoformat()))
        s = _schedule_from_created(created, next_run_at=(now - timedelta(hours=1)).isoformat())
        svc.store.update_schedule(s, expected_version=1)
        results = svc.tick(tenant_id="tenant-a")
        self.assertEqual(results[0]["status"], OCC_SKIPPED)

    def test_misfire_run_once(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now, dispatch_fn=lambda **k: {"run_id": "r1"})
        created = svc.create_schedule(_ctx(), _base_payload(misfire_policy=MISFIRE_RUN_ONCE, start_at=(now - timedelta(hours=2)).isoformat()))
        s = _schedule_from_created(created, next_run_at=(now - timedelta(hours=1)).isoformat())
        svc.store.update_schedule(s, expected_version=1)
        results = svc.tick(tenant_id="tenant-a")
        self.assertEqual(results[0]["status"], OCC_DISPATCHED)

    def test_duplicate_dispatch_idempotent(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now, dispatch_fn=lambda **k: {"run_id": "r1"})
        created = svc.create_schedule(_ctx(), _base_payload(start_at=(now - timedelta(minutes=5)).isoformat()))
        s = _schedule_from_created(created, next_run_at=(now - timedelta(minutes=1)).isoformat())
        svc.store.update_schedule(s, expected_version=1)
        scheduled_for = parse_utc(s.next_run_at or now.isoformat())
        occ = svc._materialize_occurrence(s, scheduled_for=scheduled_for)
        occ = type(occ)(**{**occ.__dict__, "status": OCC_DISPATCHED})
        svc.store.save_occurrence(occ)
        results = svc.tick(tenant_id="tenant-a")
        self.assertTrue(any(r.get("status") == "idempotent" for r in results))

    def test_hitl_waiting_approval(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now)
        created = svc.create_schedule(_ctx(), _base_payload(target_payload={"requires_approval": True, "question_type": "sales_week"}, start_at=(now - timedelta(minutes=1)).isoformat()))
        s = _schedule_from_created(created, next_run_at=now.isoformat())
        svc.store.update_schedule(s, expected_version=1)
        results = svc.tick(tenant_id="tenant-a")
        self.assertEqual(results[0]["status"], OCC_WAITING_APPROVAL)

    def test_overlap_forbid(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now, dispatch_fn=lambda **k: {"run_id": "r1"})
        created = svc.create_schedule(_ctx(), _base_payload(start_at=(now - timedelta(minutes=1)).isoformat()))
        s = _schedule_from_created(created, next_run_at=now.isoformat())
        svc.store.update_schedule(s, expected_version=1)
        svc._running[("tenant-a", created["schedule_id"])] = "occ-running"
        results = svc.tick(tenant_id="tenant-a")
        self.assertEqual(results, [])


class GovernanceTests(unittest.TestCase):
    def test_tenant_isolation_read(self):
        svc = _svc()
        created = svc.create_schedule(_ctx(), _base_payload())
        with self.assertRaises(ScheduledAutomationError) as cm:
            svc.get_schedule(_ctx(tenant="tenant-b"), tenant_id="tenant-a", schedule_id=created["schedule_id"])
        self.assertEqual(cm.exception.code, TENANT_SCOPE_VIOLATION)

    def test_tenant_isolation_run_now(self):
        svc = _svc()
        created = svc.create_schedule(_ctx(), _base_payload())
        with self.assertRaises(ScheduledAutomationError):
            svc.run_now(_ctx(tenant="tenant-b"), tenant_id="tenant-a", schedule_id=created["schedule_id"])

    def test_revoked_capability_blocks_tick(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now, caps=set())
        created = svc.create_schedule(_ctx(), _base_payload(required_capabilities=[], start_at=(now - timedelta(minutes=1)).isoformat()))
        s = _schedule_from_created(created, next_run_at=now.isoformat(), required_capabilities=("missing.cap",))
        svc.store.update_schedule(s, expected_version=1)
        results = svc.tick(tenant_id="tenant-a")
        self.assertEqual(results[0]["status"], "BLOCKED")

    def test_budget_blocked(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now, budget_ok=False)
        created = svc.create_schedule(_ctx(), _base_payload(start_at=(now - timedelta(minutes=1)).isoformat()))
        s = _schedule_from_created(created, next_run_at=now.isoformat())
        svc.store.update_schedule(s, expected_version=1)
        results = svc.tick(tenant_id="tenant-a")
        self.assertEqual(results[0]["status"], "BLOCKED")

    def test_viewer_cannot_create(self):
        svc = _svc()
        with self.assertRaises(ScheduledAutomationError) as cm:
            svc.create_schedule(_ctx(roles=("viewer",)), _base_payload())
        self.assertEqual(cm.exception.code, FORBIDDEN)

    def test_run_now_governed(self):
        svc = _svc()
        created = svc.create_schedule(_ctx(), _base_payload())
        out = svc.run_now(_ctx(), tenant_id="tenant-a", schedule_id=created["schedule_id"])
        self.assertFalse(out["mutation"] is False and "occurrence" not in out)
        self.assertIn("occurrence", out)


class RecoveryTests(unittest.TestCase):
    def test_sqlite_restart_recovery(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        path = os.path.join(os.getcwd(), ".pytest_cache", "sched_recovery_test.sqlite")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        try:
            store1 = SqliteScheduleAutomationStore(path)
            svc1 = ScheduledAutomationService(store=store1, clock=Clock(now=now), dispatcher=ScheduledAutomationDispatcher(dispatch_fn=lambda **k: {"run_id": "r1"}))
            created = svc1.create_schedule(_ctx(), _base_payload(start_at=(now - timedelta(minutes=1)).isoformat()))
            s = _schedule_from_created(created, next_run_at=now.isoformat())
            svc1.store.update_schedule(s, expected_version=1)
            del svc1
            del store1
            store2 = SqliteScheduleAutomationStore(path)
            svc2 = ScheduledAutomationService(store=store2, clock=Clock(now=now), dispatcher=ScheduledAutomationDispatcher(dispatch_fn=lambda **k: {"run_id": "r1"}))
            got = svc2.get_schedule(_ctx(), tenant_id="tenant-a", schedule_id=created["schedule_id"])
            self.assertEqual(got["schedule_id"], created["schedule_id"])
            results = svc2.tick(tenant_id="tenant-a")
            self.assertTrue(results)
            del svc2
            del store2
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class BATests(unittest.TestCase):
    def test_ba_schedule_intent(self):
        svc = _svc()
        ba = BusinessAssistantService(scheduled_automation=svc)
        ex = type("Ex", (), {"tenant_id": "tenant-a", "artifacts": [], "cost": __import__("decimal").Decimal("0"), "execution_id": "x", "workflow_id": "w", "_schedule_intent": {"daily_time": "09:00", "name": "Daily report"}})()
        step = type("S", (), {"name": "schedule_intent", "capability": "schedule"})()
        out = ba._execute_step(ex, None, type("R", (), {"text": ""})(), step)
        self.assertFalse(out["mutation"])
        self.assertIn("proposal", out)

    def test_ba_governed_create(self):
        svc = _svc()
        ba = BusinessAssistantService(scheduled_automation=svc)
        ex = type("Ex", (), {"tenant_id": "tenant-a", "artifacts": [], "cost": __import__("decimal").Decimal("0"), "execution_id": "x", "workflow_id": "w", "_schedule_intent": {"name": "Morning check", "start_at": "2026-02-01T09:00:00+00:00", "schedule_type": SCHEDULE_ONCE, "interval_seconds": None}})()
        step = type("S", (), {"name": "schedule_create", "capability": "schedule"})()
        out = ba._execute_step(ex, None, type("R", (), {"text": ""})(), step)
        self.assertTrue(out["mutation"])
        self.assertIn("schedule", out)


class AnalyticsTests(unittest.TestCase):
    def test_analytics_snapshot(self):
        svc = _svc()
        svc.create_schedule(_ctx(), _base_payload())
        snap = svc.analytics_snapshot(tenant_id="tenant-a")
        self.assertGreaterEqual(snap["total_schedules"], 1)
        self.assertIn("enabled_schedules", snap)


class AuditTests(unittest.TestCase):
    def test_audit_on_create_and_dispatch(self):
        svc = _svc(now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), dispatch_fn=lambda **k: {"run_id": "r1"})
        created = svc.create_schedule(_ctx(), _base_payload(start_at="2026-01-01T11:00:00+00:00"))
        audit = svc.store.list_audit(tenant_id="tenant-a", schedule_id=created["schedule_id"])
        self.assertTrue(any(a["event_type"] == "schedule_created" for a in audit))
        s = _schedule_from_created(created, next_run_at="2026-01-01T11:30:00+00:00")
        svc.store.update_schedule(s, expected_version=1)
        svc.tick(tenant_id="tenant-a")
        self.assertTrue(svc.observability.events)


class LiveBoundaryTests(unittest.TestCase):
    def test_live_fail_closed(self):
        svc = _svc()
        old = os.environ.get("SCHEDULED_AUTOMATION_MODE")
        os.environ["SCHEDULED_AUTOMATION_MODE"] = "LIVE"
        os.environ["SCHEDULED_AUTOMATION_LIVE_ENABLED"] = "true"
        try:
            with self.assertRaises(ScheduledAutomationError) as cm:
                svc.create_schedule(_ctx(), _base_payload())
            self.assertEqual(cm.exception.code, LIVE_FALLBACK_FORBIDDEN)
        finally:
            if old is None:
                os.environ.pop("SCHEDULED_AUTOMATION_MODE", None)
            else:
                os.environ["SCHEDULED_AUTOMATION_MODE"] = old
            os.environ.pop("SCHEDULED_AUTOMATION_LIVE_ENABLED", None)


class APITests(unittest.TestCase):
    def setUp(self):
        self._env = _auth_env()
        for k, v in self._env.items():
            os.environ[k] = v
        configure_security()
        svc = _svc()
        app = FastAPI()
        app.include_router(configure_scheduled_automation_router(svc))
        self.client = TestClient(app)
        self.svc = svc

    def test_api_create_list_get(self):
        body = {
            "tenant_id": "tenant-a",
            "name": "api-test",
            "schedule_type": SCHEDULE_INTERVAL,
            "timezone": "UTC",
            "start_at": "2026-01-01T00:00:00+00:00",
            "interval_seconds": 120,
            "target_type": TARGET_ANALYTICS,
            "target_payload": {"question_type": "sales_week"},
        }
        r = self.client.post("/api/v1/automations/schedules", json=body, headers=_headers("secret-a"))
        self.assertEqual(r.status_code, 200)
        sid = r.json()["schedule_id"]
        r2 = self.client.get("/api/v1/automations/schedules", params={"tenant_id": "tenant-a"}, headers=_headers("secret-a"))
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["items"])
        r3 = self.client.get(f"/api/v1/automations/schedules/{sid}", params={"tenant_id": "tenant-a"}, headers=_headers("secret-a"))
        self.assertEqual(r3.status_code, 200)

    def test_api_lifecycle_and_runs(self):
        body = {
            "tenant_id": "tenant-a",
            "name": "life",
            "schedule_type": SCHEDULE_ONCE,
            "timezone": "UTC",
            "start_at": "2026-03-01T10:00:00+00:00",
            "target_type": TARGET_ANALYTICS,
            "target_payload": {},
        }
        sid = self.client.post("/api/v1/automations/schedules", json=body, headers=_headers("secret-a")).json()["schedule_id"]
        self.client.post(f"/api/v1/automations/schedules/{sid}/pause", params={"tenant_id": "tenant-a"}, headers=_headers("secret-a"))
        self.client.post(f"/api/v1/automations/schedules/{sid}/resume", params={"tenant_id": "tenant-a"}, headers=_headers("secret-a"))
        self.client.post(f"/api/v1/automations/schedules/{sid}/run-now", params={"tenant_id": "tenant-a"}, headers=_headers("secret-a"))
        runs = self.client.get(f"/api/v1/automations/schedules/{sid}/runs", params={"tenant_id": "tenant-a"}, headers=_headers("secret-a"))
        self.assertEqual(runs.status_code, 200)

    def test_api_cross_tenant_denied(self):
        body = {
            "tenant_id": "tenant-a",
            "name": "x",
            "schedule_type": SCHEDULE_ONCE,
            "timezone": "UTC",
            "start_at": "2026-03-01T10:00:00+00:00",
            "target_type": TARGET_ANALYTICS,
            "target_payload": {},
        }
        sid = self.client.post("/api/v1/automations/schedules", json=body, headers=_headers("secret-a")).json()["schedule_id"]
        r = self.client.get(f"/api/v1/automations/schedules/{sid}", params={"tenant_id": "tenant-a"}, headers=_headers("secret-b"))
        self.assertEqual(r.status_code, 403)

    def test_status_endpoint(self):
        r = self.client.get("/api/v1/automations/status")
        self.assertTrue(r.json()["engineering_ready"])
        self.assertFalse(r.json()["live_active"])


class TraceTests(unittest.TestCase):
    def test_trace_metadata_in_dispatch(self):
        captured = {}

        def dispatch_fn(**kwargs):
            captured.update(kwargs)
            return {"run_id": "trace-1"}

        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        svc = _svc(now=now, dispatch_fn=dispatch_fn)
        created = svc.create_schedule(_ctx(), _base_payload(start_at=(now - timedelta(minutes=1)).isoformat()))
        s = _schedule_from_created(created, next_run_at=now.isoformat())
        svc.store.update_schedule(s, expected_version=1)
        svc.tick(tenant_id="tenant-a")
        self.assertIn("metadata", captured)
        self.assertEqual(captured["metadata"]["trigger"], "scheduled")
        self.assertIn("schedule_id", captured["metadata"])


if __name__ == "__main__":
    unittest.main()
