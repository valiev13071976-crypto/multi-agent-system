"""Deterministic Email FIXTURE adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.email.catalog import GLOBAL_EMAIL_STORE, EmailMailboxStore
from integrations.email.errors import (
    EmailAmbiguousRecipientError,
    EmailAttachmentError,
    EmailNotFoundError,
    EmailUncertainWriteOutcomeError,
    EmailUnsupportedCapabilityError,
)
from integrations.email.mapping import build_preview, fingerprint_email, validate_attachment_ref, validate_recipients


@dataclass
class EmailFixtureState(FixtureAdapterState):
    uncertain_write: bool = False
    force_ambiguous_recipient: bool = False
    tenant_override: str = ""


class EmailFixtureAdapter(FixtureProviderAdapter):
    def __init__(self, *, state: EmailFixtureState | None = None, store: EmailMailboxStore | None = None):
        super().__init__("email", state=state or EmailFixtureState())
        self.state: EmailFixtureState = self.state  # type: ignore[assignment]
        self._store = store or GLOBAL_EMAIL_STORE
        self.environment = "FIXTURE"
        self.live = False

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if not base.get("ok"):
            return base
        return {
            **base,
            "provider_identity": "fixture:email",
            "capabilities": ["email.read", "email.draft.write", "email.send"],
        }

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        params = params or {}
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(params.get("operation") or "").strip()

        if operation == "message_search":
            return self._store.search(
                tenant_id=tenant,
                query=str(params.get("query") or ""),
                page=int(params.get("page") or 1),
                page_size=int(params.get("page_size") or 5),
            )
        if operation == "message_read":
            msg = self._store.get_message(tenant_id=tenant, message_id=str(params.get("message_id") or ""))
            if not msg:
                raise EmailNotFoundError("message_not_found")
            return msg
        if operation == "thread_read":
            thread_id = str(params.get("thread_id") or "")
            items = self._store.thread_messages(tenant_id=tenant, thread_id=thread_id)
            if not items:
                raise EmailNotFoundError("thread_not_found")
            return {"thread_id": thread_id, "messages": items, "mode": "FIXTURE", "live": False}
        if operation == "mailbox_info":
            return {"mailbox": self._store.mailbox(tenant), "mode": "FIXTURE", "live": False}

        page = int(params.get("page") or 1)
        if page > self.state.max_pages:
            return {"items": [], "next_page": None, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}
        self.state.pages_served += 1
        return self._store.search(tenant_id=tenant, page=page)

    def write(
        self,
        *,
        capability: str,
        payload: dict,
        idempotency_key: str,
        tenant_id: str = "",
        credential_ref: str = "",
    ) -> dict:
        self._raise_if_bad()
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(payload.get("operation") or "generic").strip()

        if idempotency_key in self.state.writes:
            cached = dict(self.state.writes[idempotency_key])
            cached["idempotent"] = True
            return cached

        if capability == "email.draft.write" or operation == "draft_create":
            return self._write_draft(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)

        if operation == "reconcile_send":
            return self._write_reconcile_send(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)

        if capability != "email.send" and operation not in {"send", "reply", "forward"}:
            raise EmailUnsupportedCapabilityError(f"unsupported_email_write:{capability}")

        if self.state.uncertain_write and operation not in {"reconcile_send"}:
            raise EmailUncertainWriteOutcomeError("uncertain_write_outcome")

        return self._write_send(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)

    def _write_draft(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        validate_recipients(
            to=list(payload.get("to") or []),
            cc=list(payload.get("cc") or []),
            bcc=list(payload.get("bcc") or []),
            ambiguous=self.state.force_ambiguous_recipient or bool(payload.get("ambiguous_recipient")),
        )
        for att in payload.get("attachments") or []:
            validate_attachment_ref(attachment_ref=str(att.get("ref") or att), tenant_id=tenant)
        draft = self._store.create_draft(tenant_id=tenant, payload=payload)
        out = {
            "status": "DRAFT_CREATED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "draft_create",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "draft": draft,
            "sent": False,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_send(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        validate_recipients(
            to=list(payload.get("to") or []),
            cc=list(payload.get("cc") or []),
            bcc=list(payload.get("bcc") or []),
            ambiguous=self.state.force_ambiguous_recipient or bool(payload.get("ambiguous_recipient")),
        )
        for att in payload.get("attachments") or []:
            if isinstance(att, dict):
                validate_attachment_ref(attachment_ref=str(att.get("ref") or ""), tenant_id=tenant)
            else:
                validate_attachment_ref(attachment_ref=str(att), tenant_id=tenant)
        preview = payload.get("preview") or build_preview(
            operation="send",
            before=None,
            after={"to": payload.get("to"), "subject": payload.get("subject")},
        )
        fp = fingerprint_email(
            to=list(payload.get("to") or []),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            attachments=[str(a.get("ref") if isinstance(a, dict) else a) for a in (payload.get("attachments") or [])],
        )
        sent = self._store.send_message(tenant_id=tenant, payload=payload)
        out = {
            "status": "SENT",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "send",
            "mode": "FIXTURE",
            "live": False,
            "verified": sent.get("verified", "ACCEPTED"),
            "idempotent": False,
            "preview": preview,
            "fingerprint": fp,
            "send": sent,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_reconcile_send(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        expected_subject = str(payload.get("subject") or "")
        last = self._store.last_sent(tenant_id=tenant)
        verified = "VERIFIED" if last and last.get("subject") == expected_subject else "UNKNOWN"
        out = {
            "status": "RECONCILED",
            "operation": "reconcile_send",
            "verified": verified,
            "observed": last,
            "expected_subject": expected_subject,
            "mode": "FIXTURE",
            "live": False,
            "idempotent": False,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out
