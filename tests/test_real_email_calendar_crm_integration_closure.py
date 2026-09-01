"""Real Email / Calendar / CRM integration — closure tests."""

from __future__ import annotations

import unittest
from decimal import Decimal

from business_assistant.models import STATUS_WAITING_FOR_APPROVAL
from business_assistant.service import BusinessAssistantService
from integrations.activation.errors import (
    IntegrationCrossTenantError,
    IntegrationLiveFallbackForbiddenError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationWriteDeniedError,
)
from integrations.activation.models import ENV_FIXTURE, ENV_LIVE
from integrations.activation.service import IntegrationActivationService
from integrations.calendar.catalog import CalendarStore
from integrations.calendar.config import calendar_live_active, calendar_live_verified
from integrations.calendar.errors import CalendarAmbiguousTargetError, CalendarTimezoneError, CalendarUncertainWriteOutcomeError
from integrations.calendar.fixture_adapter import CalendarFixtureAdapter, CalendarFixtureState
from integrations.calendar.live_adapter import LiveCalendarAdapter
from integrations.crm.catalog import CrmStore
from integrations.crm.config import crm_live_active, crm_live_verified
from integrations.crm.errors import CrmAmbiguousTargetError, CrmDuplicateCandidateError, CrmUncertainWriteOutcomeError, CrmUnsupportedCapabilityError
from integrations.crm.fixture_adapter import CrmFixtureAdapter
from integrations.crm.live_adapter import LiveCrmAdapter
from integrations.email.catalog import EmailMailboxStore
from integrations.email.config import email_live_active, email_live_verified
from integrations.email.errors import EmailAmbiguousRecipientError, EmailAttachmentError, EmailNotFoundError, EmailUncertainWriteOutcomeError
from integrations.email.fixture_adapter import EmailFixtureAdapter, EmailFixtureState
from integrations.email.live_adapter import LiveEmailAdapter
from integrations.email.mapping import fingerprint_email
from integrations.calendar.mapping import fingerprint_event
from integrations.productivity import email_calendar_crm_engineering_ready


def _svc(
    email_store: EmailMailboxStore | None = None,
    cal_store: CalendarStore | None = None,
    crm_store: CrmStore | None = None,
) -> IntegrationActivationService:
    svc = IntegrationActivationService()
    if email_store is not None:
        a = EmailFixtureAdapter(store=email_store)
        svc._adapters["email"] = a
        svc._email_fixture = a
    if cal_store is not None:
        a = CalendarFixtureAdapter(store=cal_store)
        svc._adapters["calendar"] = a
        svc._calendar_fixture = a
    if crm_store is not None:
        a = CrmFixtureAdapter(store=crm_store)
        svc._adapters["crm"] = a
        svc._crm_fixture = a
    return svc


def _active(svc: IntegrationActivationService, *, tenant: str, provider: str, env: str = ENV_FIXTURE):
    ref = svc.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:{provider}-{tenant}", value=f"tok-{tenant}")
    conn = svc.configure_connection(tenant_id=tenant, provider_id=provider, credential_ref=ref, environment=env)
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)
    return conn


class ProductivityFlagsTests(unittest.TestCase):
    def test_engineering_ready(self):
        self.assertTrue(email_calendar_crm_engineering_ready())
        self.assertFalse(email_live_active())
        self.assertFalse(email_live_verified())
        self.assertFalse(calendar_live_active())
        self.assertFalse(calendar_live_verified())
        self.assertFalse(crm_live_active())
        self.assertFalse(crm_live_verified())


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.store = EmailMailboxStore()
        self.svc = _svc(email_store=self.store)
        _active(self.svc, tenant="tenant-a", provider="email")

    def test_search_and_read(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "message_search", "query": "Price"},
        )
        self.assertEqual(out["result"]["mode"], "FIXTURE")
        self.assertTrue(out["result"]["items"])

    def test_thread_identity(self):
        adapter = EmailFixtureAdapter(store=self.store)
        out = adapter.read(
            capability="email.read",
            params={"operation": "thread_read", "thread_id": "thread-100"},
            tenant_id="tenant-a",
        )
        self.assertEqual(len(out["messages"]), 2)

    def test_draft_not_sent(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.draft.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "draft_create", "to": ["a@example.com"], "subject": "S", "body": "B"},
            idempotency_key="draft-1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["status"], "DRAFT_CREATED")
        self.assertFalse(out["result"].get("sent", True))

    def test_send_requires_write_capability(self):
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="email.send",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "send", "to": ["a@example.com"], "subject": "S", "body": "B"},
                idempotency_key="send-0",
                approved_write=False,
            )

    def test_draft_capability_no_send(self):
        conn = _active(self.svc, tenant="tenant-a", provider="email")
        conn_ro = self.svc.configure_connection(
            tenant_id="tenant-a",
            provider_id="email",
            credential_ref=conn.credential_ref,
            environment=ENV_FIXTURE,
            write_capabilities=("email.draft.write",),
        )
        self.svc.activate_connection(tenant_id="tenant-a", connection_id=conn_ro.connection_id)
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="email.send",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "send", "to": ["a@example.com"], "subject": "S", "body": "B"},
                idempotency_key="send-cap",
                approved_write=True,
                connection_id=conn_ro.connection_id,
            )

    def test_recipient_ambiguous(self):
        adapter = EmailFixtureAdapter(store=self.store, state=EmailFixtureState(force_ambiguous_recipient=True))
        with self.assertRaises(EmailAmbiguousRecipientError):
            adapter.write(
                capability="email.send",
                payload={"operation": "send", "to": ["a@example.com"], "subject": "S", "body": "B"},
                idempotency_key="amb",
                tenant_id="tenant-a",
            )

    def test_attachment_path_rejected(self):
        adapter = EmailFixtureAdapter(store=self.store)
        with self.assertRaises(EmailAttachmentError):
            adapter.write(
                capability="email.send",
                payload={"operation": "send", "to": ["a@example.com"], "subject": "S", "body": "B", "attachments": ["../../../etc/passwd"]},
                idempotency_key="att-bad",
                tenant_id="tenant-a",
            )

    def test_send_idempotent(self):
        payload = {"operation": "send", "to": ["supplier@example.com"], "subject": "Hi", "body": "Hello"}
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.send",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="send-1",
            approved_write=True,
        )
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.send",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="send-1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(self.store.write_count("send-1"), 1)

    def test_uncertain_send(self):
        self.svc._adapters["email"].state.uncertain_write = True
        with self.assertRaises(EmailUncertainWriteOutcomeError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="email.send",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "send", "to": ["a@example.com"], "subject": "S", "body": "B"},
                idempotency_key="send-u",
                approved_write=True,
            )
        with self.assertRaises(EmailUncertainWriteOutcomeError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="email.send",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "send", "to": ["a@example.com"], "subject": "S2", "body": "B2"},
                idempotency_key="send-u2",
                approved_write=True,
            )

    def test_reconcile_send(self):
        subject = "Reconcile test"
        self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.send",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "send", "to": ["a@example.com"], "subject": subject, "body": "B"},
            idempotency_key="send-rec",
            approved_write=True,
        )
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.send",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "reconcile_send", "subject": subject},
            idempotency_key="send-rec2",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_pagination_terminates(self):
        adapter = EmailFixtureAdapter(store=self.store)
        adapter.state.max_pages = 2
        p1 = adapter.read(capability="email.read", params={"page": 1}, tenant_id="tenant-a")
        p3 = adapter.read(capability="email.read", params={"page": 3}, tenant_id="tenant-a")
        self.assertEqual(p3["items"], [])
        self.assertIsNone(p3.get("next_page"))
        self.assertTrue(p1.get("bounded"))

    def test_attachment_tenant_scope(self):
        adapter = EmailFixtureAdapter(store=self.store)
        with self.assertRaises(EmailAttachmentError):
            adapter.write(
                capability="email.send",
                payload={
                    "operation": "send",
                    "to": ["a@example.com"],
                    "subject": "S",
                    "body": "B",
                    "attachments": ["file:tenant-b:secret.doc"],
                },
                idempotency_key="att-tenant",
                tenant_id="tenant-a",
            )

    def test_fingerprint_changes_on_recipient_change(self):
        fp1 = fingerprint_email(to=["a@example.com"], subject="S", body="B", attachments=[])
        fp2 = fingerprint_email(to=["b@example.com"], subject="S", body="B", attachments=[])
        self.assertNotEqual(fp1, fp2)

    def test_tenant_mailbox_isolation(self):
        a = self.store.search(tenant_id="tenant-a", query="")
        b = self.store.search(tenant_id="tenant-b", query="")
        self.assertNotEqual(a["items"][0]["mailbox"], b["items"][0]["mailbox"])


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.store = CalendarStore()
        self.svc = _svc(cal_store=self.store)
        _active(self.svc, tenant="tenant-a", provider="calendar")

    def test_event_read_with_timezone(self):
        adapter = CalendarFixtureAdapter(store=self.store)
        ev = adapter.read(
            capability="calendar.read",
            params={"operation": "event_read", "calendar_id": "cal-primary", "event_id": "evt-1"},
            tenant_id="tenant-a",
        )
        self.assertEqual(ev["timezone"], "Europe/Moscow")

    def test_free_busy(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="calendar.availability.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "free_busy", "calendar_id": "cal-primary", "start": "2026-02-01T00:00:00+03:00", "end": "2026-02-02T00:00:00+03:00"},
        )
        self.assertIn("busy", out["result"])

    def test_read_no_create(self):
        conn = _active(self.svc, tenant="tenant-a", provider="calendar")
        conn_ro = self.svc.configure_connection(
            tenant_id="tenant-a",
            provider_id="calendar",
            credential_ref=conn.credential_ref,
            environment=ENV_FIXTURE,
            write_capabilities=(),
        )
        self.svc.activate_connection(tenant_id="tenant-a", connection_id=conn_ro.connection_id)
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="calendar.event.create",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "event_create", "title": "T", "start": "2026-03-01T10:00:00+03:00", "end": "2026-03-01T11:00:00+03:00", "timezone": "Europe/Moscow"},
                idempotency_key="cal-cap",
                approved_write=True,
                connection_id=conn_ro.connection_id,
            )

    def test_event_create_idempotent(self):
        payload = {
            "operation": "event_create",
            "title": "Sync",
            "start": "2026-03-01T10:00:00+03:00",
            "end": "2026-03-01T11:00:00+03:00",
            "timezone": "Europe/Moscow",
            "attendees": ["guest@example.com"],
        }
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="calendar.event.create",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="cal-1",
            approved_write=True,
        )
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="calendar.event.create",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="cal-1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])

    def test_ambiguous_attendees(self):
        adapter = CalendarFixtureAdapter(store=self.store, state=CalendarFixtureState(force_ambiguous_attendee=True))
        with self.assertRaises(CalendarAmbiguousTargetError):
            adapter.write(
                capability="calendar.event.create",
                payload={"operation": "event_create", "title": "T", "start": "2026-03-01T10:00:00+03:00", "end": "2026-03-01T11:00:00+03:00", "timezone": "Europe/Moscow"},
                idempotency_key="cal-amb",
                tenant_id="tenant-a",
            )

    def test_naive_time_fails_closed(self):
        adapter = CalendarFixtureAdapter(store=self.store)
        with self.assertRaises(CalendarTimezoneError):
            adapter.write(
                capability="calendar.event.create",
                payload={"operation": "event_create", "title": "Bad", "start": "2026-03-01T10:00:00", "end": "2026-03-01T11:00:00", "timezone": ""},
                idempotency_key="cal-tz",
                tenant_id="tenant-a",
            )

    def test_event_cancel_governed(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="calendar.event.cancel",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "event_cancel", "calendar_id": "cal-primary", "event_id": "evt-1"},
            idempotency_key="cal-cancel",
            approved_write=True,
        )
        self.assertEqual(out["result"]["status"], "CANCELLED")

    def test_fingerprint_attendee_change(self):
        fp1 = fingerprint_event(calendar_id="cal-primary", title="T", start="2026-03-01T10:00:00+03:00", end="2026-03-01T11:00:00+03:00", timezone="Europe/Moscow", attendees=["a@example.com"])
        fp2 = fingerprint_event(calendar_id="cal-primary", title="T", start="2026-03-01T10:00:00+03:00", end="2026-03-01T11:00:00+03:00", timezone="Europe/Moscow", attendees=["b@example.com"])
        self.assertNotEqual(fp1, fp2)

    def test_reconcile_event(self):
        created = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="calendar.event.create",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "event_create", "title": "Verify Me", "start": "2026-04-01T10:00:00+03:00", "end": "2026-04-01T11:00:00+03:00", "timezone": "Europe/Moscow"},
            idempotency_key="cal-rec",
            approved_write=True,
        )
        event_id = created["result"]["event"]["event_id"]
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="calendar.event.create",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "reconcile_event", "calendar_id": "cal-primary", "event_id": event_id, "title": "Verify Me"},
            idempotency_key="cal-rec2",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")


class CrmTests(unittest.TestCase):
    def setUp(self):
        self.store = CrmStore()
        self.svc = _svc(crm_store=self.store)
        _active(self.svc, tenant="tenant-a", provider="crm")

    def test_contact_lead_deal_read(self):
        for op, cap, oid in [
            ("contact_read", "crm.contact.read", "cnt-1"),
            ("lead_read", "crm.lead.read", "lead-1"),
            ("deal_read", "crm.deal.read", "deal-1"),
        ]:
            out = self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability=cap,
                environment=ENV_FIXTURE,
                operation_class="READ",
                payload={"operation": op, "provider_id": oid, f"{op.split('_')[0]}_id": oid},
            )
            self.assertEqual(out["result"]["mode"], "FIXTURE")

    def test_ids_not_by_name_only(self):
        c1 = self.store.get(tenant_id="tenant-a", object_type="contacts", provider_id="cnt-1")
        c2 = self.store.get(tenant_id="tenant-a", object_type="contacts", provider_id="cnt-2")
        self.assertEqual(c1["name"], c2["name"])
        self.assertNotEqual(c1["provider_id"], c2["provider_id"])

    def test_patch_omitted_not_cleared(self):
        before = self.store.get(tenant_id="tenant-a", object_type="contacts", provider_id="cnt-1")
        self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="crm.contact.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "contact_update", "contact_id": "cnt-1", "patch": {"phone": "+79999999999"}},
            idempotency_key="crm-patch",
            approved_write=True,
        )
        after = self.store.get(tenant_id="tenant-a", object_type="contacts", provider_id="cnt-1")
        self.assertEqual(after["email"], before["email"])

    def test_duplicate_candidate(self):
        with self.assertRaises(CrmDuplicateCandidateError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="crm.contact.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "contact_create", "email": "ivan.a@example.com", "name": "Ivan Copy"},
                idempotency_key="crm-dup",
                approved_write=True,
            )

    def test_ambiguous_by_name(self):
        adapter = CrmFixtureAdapter(store=self.store)
        with self.assertRaises(CrmAmbiguousTargetError):
            adapter.write(
                capability="crm.contact.write",
                payload={"operation": "contact_update", "contact_id": "cnt-1", "ambiguous_name": True},
                idempotency_key="crm-amb",
                tenant_id="tenant-a",
            )

    def test_delete_unsupported(self):
        adapter = CrmFixtureAdapter(store=self.store)
        with self.assertRaises(CrmUnsupportedCapabilityError):
            adapter.write(capability="crm.write", payload={"operation": "delete"}, idempotency_key="del", tenant_id="tenant-a")

    def test_read_no_write(self):
        conn = _active(self.svc, tenant="tenant-a", provider="crm")
        conn_ro = self.svc.configure_connection(
            tenant_id="tenant-a",
            provider_id="crm",
            credential_ref=conn.credential_ref,
            environment=ENV_FIXTURE,
            write_capabilities=(),
        )
        self.svc.activate_connection(tenant_id="tenant-a", connection_id=conn_ro.connection_id)
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="crm.contact.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "contact_update", "contact_id": "cnt-1", "patch": {"phone": "1"}},
                idempotency_key="crm-cap",
                approved_write=True,
                connection_id=conn_ro.connection_id,
            )

    def test_search_bounded(self):
        adapter = CrmFixtureAdapter(store=self.store)
        out = adapter.read(capability="crm.contact.read", params={"page": 1}, tenant_id="tenant-a")
        self.assertTrue(out.get("bounded", True))

    def test_reconcile_contact(self):
        self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="crm.contact.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "contact_update", "contact_id": "cnt-1", "patch": {"phone": "+71111111111"}},
            idempotency_key="crm-rec",
            approved_write=True,
        )
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="crm.contact.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "reconcile_contact", "contact_id": "cnt-1", "expected_phone": "+71111111111"},
            idempotency_key="crm-rec2",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_uncertain_crm_write(self):
        self.svc._adapters["crm"].state.uncertain_write = True
        with self.assertRaises(CrmUncertainWriteOutcomeError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="crm.contact.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "contact_update", "contact_id": "cnt-1", "patch": {"phone": "1"}},
                idempotency_key="crm-u",
                approved_write=True,
            )


class LiveSafetyTests(unittest.TestCase):
    def test_no_fixture_fallback(self):
        svc = _svc(email_store=EmailMailboxStore())
        _active(svc, tenant="tenant-a", provider="email")
        with self.assertRaises(IntegrationLiveFallbackForbiddenError):
            svc.resolve_connection(tenant_id="tenant-a", capability="email.read", environment=ENV_LIVE)

    def test_live_email_fail_closed(self):
        with self.assertRaises(IntegrationNotConfiguredError):
            LiveEmailAdapter().read(capability="email.read", params={}, credential_ref="secret:x")

    def test_live_calendar_fail_closed(self):
        with self.assertRaises(IntegrationNotConfiguredError):
            LiveCalendarAdapter().write(capability="calendar.event.create", payload={}, idempotency_key="x")

    def test_live_crm_fail_closed(self):
        with self.assertRaises(IntegrationNotConfiguredError):
            LiveCrmAdapter().write(capability="crm.contact.write", payload={}, idempotency_key="x")


class CrossSystemBATests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc(EmailMailboxStore(), CalendarStore(), CrmStore())
        for p in ("email", "calendar", "crm"):
            _active(self.svc, tenant="tenant-a", provider=p)
        self.ba = BusinessAssistantService(integration_activation=self.svc, integration_environment=ENV_FIXTURE)

    def _mock_ex(self, **extra):
        base = {
            "tenant_id": "tenant-a",
            "artifacts": [],
            "cost": Decimal("0"),
            "execution_id": "ex-test",
            "workflow_id": "wf-test",
        }
        base.update(extra)
        return type("Ex", (), base)()

    def test_crm_lead_to_email_draft(self):
        ex = self._mock_ex(_email_send_payload=None)
        out = self.ba._execute_step(ex, None, None, type("S", (), {"name": "crm_lead_to_email_draft", "capability": "crm"})())
        self.assertEqual(out["orchestration"], "crm→email")
        self.assertFalse(out["mutation"])

    def test_crm_contact_to_calendar(self):
        ex = self._mock_ex(_calendar_event_payload=None)
        out = self.ba._execute_step(ex, None, None, type("S", (), {"name": "crm_contact_to_calendar_prep", "capability": "crm"})())
        self.assertEqual(out["orchestration"], "crm→calendar")

    def test_email_to_crm_prep(self):
        ex = self._mock_ex(_crm_update_payload=None)
        out = self.ba._execute_step(ex, None, None, type("S", (), {"name": "email_to_crm_update_prep", "capability": "email"})())
        self.assertEqual(out["orchestration"], "email→crm")

    def test_communication_governed_send(self):
        req = self.ba.submit_request(tenant_id="tenant-a", user_id="u", text="Подготовь письмо поставщику и покажи мне перед отправкой.")
        plan = self.ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = self.ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertEqual(ex.status, STATUS_WAITING_FOR_APPROVAL)
        self.assertEqual(len(self.ba._external_writes), 0)


class ObservabilityTests(unittest.TestCase):
    def test_evidence_and_usage(self):
        svc = _svc(EmailMailboxStore(), CalendarStore(), CrmStore())
        _active(svc, tenant="tenant-a", provider="email")
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.send",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "send", "to": ["a@example.com"], "subject": "S", "body": "B"},
            idempotency_key="obs-1",
            approved_write=True,
        )
        self.assertTrue(any(u["provider"] == "email" for u in svc.usage_events(tenant_id="tenant-a")))
        self.assertTrue(any(e.provider_id == "email" for e in svc.list_evidence(tenant_id="tenant-a")))


class TenantIsolationTests(unittest.TestCase):
    def test_cross_tenant_connection(self):
        svc = _svc(EmailMailboxStore())
        conn = _active(svc, tenant="tenant-a", provider="email")
        with self.assertRaises(IntegrationCrossTenantError):
            svc.get_connection(tenant_id="tenant-b", connection_id=conn.connection_id)

    def test_tenant_b_cannot_read_tenant_a_email(self):
        store = EmailMailboxStore()
        svc = _svc(email_store=store)
        _active(svc, tenant="tenant-a", provider="email")
        _active(svc, tenant="tenant-b", provider="email")
        a = svc.execute_via_gateway(tenant_id="tenant-a", capability="email.read", environment=ENV_FIXTURE, operation_class="READ", payload={"operation": "message_read", "message_id": "msg-1001"})
        b = svc.execute_via_gateway(tenant_id="tenant-b", capability="email.read", environment=ENV_FIXTURE, operation_class="READ", payload={"operation": "message_read", "message_id": "msg-1001"})
        self.assertNotEqual(a["result"]["mailbox"], b["result"]["mailbox"])
        self.assertNotEqual(a["result"]["message_id"], b["result"]["message_id"])

    def test_tenant_calendar_isolation(self):
        store = CalendarStore()
        a = store.get_event(tenant_id="tenant-a", calendar_id="cal-primary", event_id="evt-1")
        b = store.get_event(tenant_id="tenant-b", calendar_id="cal-primary", event_id="evt-1")
        self.assertNotEqual(a["event_id"], b["event_id"])
        self.assertEqual(b["calendar_id"], "cal-b-primary")

    def test_tenant_crm_isolation(self):
        store = CrmStore()
        a = store.get(tenant_id="tenant-a", object_type="contacts", provider_id="cnt-1")
        b = store.get(tenant_id="tenant-b", object_type="contacts", provider_id="cnt-1")
        self.assertNotEqual(a.get("email"), b.get("email"))


class SecurityTests(unittest.TestCase):
    def test_secrets_not_in_evidence(self):
        svc = _svc(EmailMailboxStore(), CalendarStore(), CrmStore())
        ref = svc.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:email-a", value="super-secret-token-xyz")
        conn = svc.configure_connection(tenant_id="tenant-a", provider_id="email", credential_ref=ref, environment=ENV_FIXTURE)
        svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="email.send",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "send", "to": ["a@example.com"], "subject": "S", "body": "B"},
            idempotency_key="sec-1",
            approved_write=True,
        )
        svc.assert_no_secrets_in_evidence(tenant_id="tenant-a")

    def test_malicious_email_no_send(self):
        svc = _svc(EmailMailboxStore(), CalendarStore(), CrmStore())
        for p in ("email", "calendar", "crm"):
            _active(svc, tenant="tenant-a", provider=p)
        ba = BusinessAssistantService(integration_activation=svc, integration_environment=ENV_FIXTURE)
        ex = type(
            "Ex",
            (),
            {"tenant_id": "tenant-a", "artifacts": [], "findings": [], "cost": Decimal("0"), "execution_id": "x", "workflow_id": "w"},
        )()
        step = type("S", (), {"name": "retrieve_email_context", "capability": "email"})()
        out = ba._execute_step(ex, None, type("R", (), {"text": ""})(), step)
        self.assertFalse(out.get("mutation", True))
        self.assertEqual(len(ba._external_writes), 0)
        self.assertTrue(any("Untrusted" in f.summary for f in ex.findings))


class RateLimitTests(unittest.TestCase):
    def test_email_rate_limit(self):
        svc = _svc(EmailMailboxStore())
        _active(svc, tenant="tenant-a", provider="email")
        svc.adapter_state("email").rate_limited = True
        with self.assertRaises(IntegrationRateLimitedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="email.read",
                environment=ENV_FIXTURE,
                operation_class="READ",
            )


class NoBypassTests(unittest.TestCase):
    def test_separate_adapters(self):
        svc = _svc(EmailMailboxStore(), CalendarStore(), CrmStore())
        self.assertNotEqual(type(svc._adapter_for_provider("email")), type(svc._adapter_for_provider("crm")))
        self.assertNotEqual(type(svc._adapter_for_provider("calendar")), type(svc._adapter_for_provider("crm")))


if __name__ == "__main__":
    unittest.main()
