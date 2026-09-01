"""Deterministic CRM FIXTURE adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.crm.catalog import GLOBAL_CRM_STORE, CrmStore
from integrations.crm.errors import (
    CrmAmbiguousTargetError,
    CrmNotFoundError,
    CrmUncertainWriteOutcomeError,
    CrmUnsupportedCapabilityError,
)
from integrations.crm.mapping import build_preview, check_duplicate_policy, fingerprint_crm


@dataclass
class CrmFixtureState(FixtureAdapterState):
    uncertain_write: bool = False
    tenant_override: str = ""


class CrmFixtureAdapter(FixtureProviderAdapter):
    def __init__(self, *, state: CrmFixtureState | None = None, store: CrmStore | None = None):
        super().__init__("crm", state=state or CrmFixtureState())
        self.state: CrmFixtureState = self.state  # type: ignore[assignment]
        self._store = store or GLOBAL_CRM_STORE
        self.environment = "FIXTURE"
        self.live = False

    def verify(self, *, credential_ref: str) -> dict:
        base = super().verify(credential_ref=credential_ref)
        if not base.get("ok"):
            return base
        return {
            **base,
            "provider_identity": "fixture:crm",
            "capabilities": [
                "crm.contact.read",
                "crm.contact.write",
                "crm.lead.read",
                "crm.deal.read",
                "crm.activity.write",
            ],
        }

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        params = params or {}
        tenant = tenant_id or self.state.tenant_override or "tenant-a"
        operation = str(params.get("operation") or "").strip()

        if operation == "contact_read":
            obj = self._store.get(tenant_id=tenant, object_type="contacts", provider_id=str(params.get("provider_id") or params.get("contact_id") or ""))
            if not obj:
                raise CrmNotFoundError("contact_not_found")
            return obj
        if operation == "lead_read":
            obj = self._store.get(tenant_id=tenant, object_type="leads", provider_id=str(params.get("provider_id") or params.get("lead_id") or ""))
            if not obj:
                raise CrmNotFoundError("lead_not_found")
            return obj
        if operation == "deal_read":
            obj = self._store.get(tenant_id=tenant, object_type="deals", provider_id=str(params.get("provider_id") or params.get("deal_id") or ""))
            if not obj:
                raise CrmNotFoundError("deal_not_found")
            return obj
        if operation == "contact_search":
            return self._store.search(
                tenant_id=tenant,
                object_type="contacts",
                query=str(params.get("query") or ""),
                page=int(params.get("page") or 1),
            )
        if operation == "duplicate_check":
            matches = self._store.find_duplicates(tenant_id=tenant, email=str(params.get("email") or ""), phone=str(params.get("phone") or ""))
            return {**check_duplicate_policy(matches=matches), "matches": matches, "mode": "FIXTURE", "live": False}

        object_type = {"crm.lead.read": "leads", "crm.deal.read": "deals"}.get(capability, "contacts")
        return self._store.search(tenant_id=tenant, object_type=object_type, page=int(params.get("page") or 1))

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
        operation = str(payload.get("operation") or "").strip()

        if idempotency_key in self.state.writes:
            cached = dict(self.state.writes[idempotency_key])
            cached["idempotent"] = True
            return cached

        if operation in {"delete", "merge"} or payload.get("operation") == "delete":
            raise CrmUnsupportedCapabilityError("crm_delete_not_supported")

        if operation == "reconcile_contact":
            return self._write_reconcile_contact(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)

        if self.state.uncertain_write and operation not in {"reconcile_contact"}:
            raise CrmUncertainWriteOutcomeError("uncertain_write_outcome")

        if operation == "contact_update" or payload.get("provider_id") or payload.get("contact_id"):
            return self._write_contact_update(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)
        if operation == "contact_create" or (capability == "crm.contact.write" and not payload.get("provider_id")):
            return self._write_contact_create(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)
        if operation == "activity_create":
            return self._write_activity(tenant=tenant, capability=capability, payload=payload, idempotency_key=idempotency_key)
        raise CrmUnsupportedCapabilityError(f"unsupported_crm_write:{capability}")

    def _write_contact_create(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        email = str(payload.get("email") or "")
        matches = self._store.find_duplicates(tenant_id=tenant, email=email)
        dup = check_duplicate_policy(matches=matches, allow_create=bool(payload.get("force_create")))
        if dup.get("status") == "DUPLICATE_CANDIDATE" and not payload.get("force_create"):
            from integrations.crm.errors import CrmDuplicateCandidateError

            raise CrmDuplicateCandidateError("duplicate_candidate")
        contact = self._store.create_contact(tenant_id=tenant, payload=payload)
        out = {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "contact_create",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "contact": contact,
            "duplicate_check": dup,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_contact_update(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        pid = str(payload.get("provider_id") or payload.get("contact_id") or "")
        if payload.get("ambiguous_name"):
            raise CrmAmbiguousTargetError("ambiguous_contact_by_name")
        patch = dict(payload.get("patch") or payload.get("fields") or {})
        if not patch and payload.get("phone"):
            patch = {"phone": payload.get("phone")}
        existing = self._store.get(tenant_id=tenant, object_type="contacts", provider_id=pid)
        if not existing:
            raise CrmNotFoundError("contact_not_found")
        preview = payload.get("preview") or build_preview(operation="contact_update", before=existing, after=patch)
        contact = self._store.patch_contact(tenant_id=tenant, provider_id=pid, patch=patch)
        fp = fingerprint_crm(object_type="contact", provider_id=pid, patch=patch)
        out = {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "contact_update",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "preview": preview,
            "contact": contact,
            "fingerprint": fp,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_activity(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        out = {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "operation": "activity_create",
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "activity_id": f"act-{uuid.uuid4().hex[:6]}",
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out

    def _write_reconcile_contact(self, *, tenant: str, capability: str, payload: dict, idempotency_key: str) -> dict:
        pid = str(payload.get("contact_id") or payload.get("provider_id") or "")
        expected_phone = str(payload.get("expected_phone") or "")
        contact = self._store.get(tenant_id=tenant, object_type="contacts", provider_id=pid)
        verified = "VERIFIED" if contact and str(contact.get("phone") or "") == expected_phone else "UNKNOWN"
        out = {
            "status": "RECONCILED",
            "operation": "reconcile_contact",
            "verified": verified,
            "observed": contact,
            "mode": "FIXTURE",
            "live": False,
            "idempotent": False,
            "external_write_count": self._store.record_write(idempotency_key),
        }
        self.state.writes[idempotency_key] = out
        return out
