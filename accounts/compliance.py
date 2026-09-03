"""Compliance foundation — inventory, policies, consent, retention, processors."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from accounts.models import (
    AGE_POLICY_STATUS,
    COOKIE_STRICTLY_NECESSARY,
    COOKIE_UNKNOWN,
    DEC_MARKETING,
    DEC_OPTIONAL_ANALYTICS,
    DEC_PERSONAL_DATA,
    DEC_PRIVACY_ACK,
    DEC_TERMS,
    DOC_AI_DISCLOSURE,
    DOC_CONSENT_TEXT,
    DOC_PERSONAL_DATA,
    DOC_PRIVACY,
    DOC_TERMS,
    AccountsAuditEvent,
    DeletionRequestRecord,
    PolicyDocument,
    UserDecisionRecord,
)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DataInventoryEntry:
    data_category: str
    purpose: str
    legal_basis_class: str
    required_or_optional: str
    retention_class: str
    data_owner: str
    storage_location: str
    processor_class: str
    deletion_behavior: str
    export_behavior: str
    audit_requirement: str


DATA_INVENTORY: tuple[DataInventoryEntry, ...] = (
    DataInventoryEntry(
        "account_credentials",
        "authentication",
        "CONTRACT_OR_LEGITIMATE_INTEREST_CLASS",
        "REQUIRED",
        "ACCOUNT_LIFECYCLE",
        "controller",
        "accounts_db",
        "HOSTING",
        "DELETE_HASH_ON_ACCOUNT_DELETE",
        "EXCLUDE_HASH",
        "REQUIRED",
    ),
    DataInventoryEntry(
        "account_profile",
        "account_management",
        "CONTRACT_CLASS",
        "REQUIRED",
        "ACCOUNT_LIFECYCLE",
        "controller",
        "accounts_db",
        "HOSTING",
        "ANONYMIZE_OR_DELETE",
        "EXPORTABLE",
        "REQUIRED",
    ),
    DataInventoryEntry(
        "session_records",
        "security",
        "LEGITIMATE_INTEREST_CLASS",
        "REQUIRED",
        "SESSION_SHORT",
        "controller",
        "accounts_db",
        "HOSTING",
        "DELETE_ON_LOGOUT_OR_EXPIRY",
        "EXCLUDE",
        "REQUIRED",
    ),
    DataInventoryEntry(
        "subscription_state",
        "subscription_management",
        "CONTRACT_CLASS",
        "OPTIONAL",
        "BILLING_RETENTION",
        "controller",
        "saas_db",
        "HOSTING",
        "RETAIN_PER_POLICY",
        "EXPORTABLE_SAFE",
        "REQUIRED",
    ),
    DataInventoryEntry(
        "usage_meters",
        "usage_accounting",
        "CONTRACT_CLASS",
        "REQUIRED",
        "USAGE_RETENTION",
        "controller",
        "accounts_db",
        "HOSTING",
        "AGGREGATE_THEN_DELETE",
        "EXPORTABLE_SAFE",
        "OPTIONAL",
    ),
    DataInventoryEntry(
        "consent_evidence",
        "compliance_evidence",
        "LEGAL_OBLIGATION_CLASS",
        "REQUIRED",
        "CONSENT_EVIDENCE_RETENTION",
        "controller",
        "accounts_db",
        "HOSTING",
        "RETAIN_PER_POLICY",
        "EXPORTABLE",
        "REQUIRED",
    ),
    DataInventoryEntry(
        "conversation_content",
        "service_operation",
        "CONTRACT_CLASS",
        "OPTIONAL",
        "CONTENT_RETENTION_UNRESOLVED",
        "controller",
        "ba_api_db",
        "HOSTING_AND_LLM_PROCESSOR",
        "USER_REQUEST_GOVERNED",
        "EXPORTABLE_SCOPED",
        "REQUIRED",
    ),
)


@dataclass(frozen=True)
class RetentionClass:
    retention_class: str
    description: str
    period_status: str  # CONFIGURED | REQUIRES_PRODUCT_LEGAL_DECISION
    default_days: int | None


RETENTION_CLASSES: tuple[RetentionClass, ...] = (
    RetentionClass("SESSION_SHORT", "Auth sessions", "CONFIGURED", 14),
    RetentionClass("ACCOUNT_LIFECYCLE", "Account records", "REQUIRES_PRODUCT_LEGAL_DECISION", None),
    RetentionClass("BILLING_RETENTION", "Billing/subscription evidence", "REQUIRES_PRODUCT_LEGAL_DECISION", None),
    RetentionClass("USAGE_RETENTION", "Usage meters", "REQUIRES_PRODUCT_LEGAL_DECISION", None),
    RetentionClass("CONSENT_EVIDENCE_RETENTION", "Consent/decision evidence", "REQUIRES_PRODUCT_LEGAL_DECISION", None),
    RetentionClass("CONTENT_RETENTION_UNRESOLVED", "Chat/files content", "REQUIRES_PRODUCT_LEGAL_DECISION", None),
    RetentionClass("AUDIT_RETENTION", "Security/admin audit", "REQUIRES_PRODUCT_LEGAL_DECISION", None),
)


@dataclass(frozen=True)
class CookieInventoryEntry:
    name: str
    classification: str
    purpose: str
    storage: str


COOKIE_INVENTORY: tuple[CookieInventoryEntry, ...] = (
    CookieInventoryEntry("panda_session", COOKIE_STRICTLY_NECESSARY, "human authentication session", "cookie"),
    CookieInventoryEntry("panda_csrf", COOKIE_STRICTLY_NECESSARY, "CSRF protection twin", "cookie"),
    CookieInventoryEntry("panda_api_key", COOKIE_UNKNOWN, "legacy workspace API key (sessionStorage)", "sessionStorage"),
)


@dataclass(frozen=True)
class ProcessorInventoryEntry:
    service_class: str
    data_categories: tuple[str, ...]
    purpose: str
    destination_region: str
    transfer_allowed_policy: str
    minimization_rule: str


PROCESSOR_INVENTORY: tuple[ProcessorInventoryEntry, ...] = (
    ProcessorInventoryEntry(
        "HOSTING",
        ("account_profile", "session_records", "usage_meters"),
        "application hosting",
        "UNKNOWN",
        "REQUIRES_LEGAL_REVIEW",
        "store only required operational fields",
    ),
    ProcessorInventoryEntry(
        "LLM_PROVIDER",
        ("conversation_content",),
        "AI inference",
        "UNKNOWN",
        "REQUIRES_LEGAL_REVIEW",
        "send only request-scoped prompts; no passwords/secrets",
    ),
    ProcessorInventoryEntry(
        "PAYMENT_PROVIDER",
        ("subscription_state",),
        "hosted checkout / recurring billing",
        "UNKNOWN",
        "REQUIRES_LEGAL_REVIEW",
        "never send full card/CVV; provider refs only",
    ),
)


class ComplianceService:
    def __init__(self, *, store):
        self.store = store
        self._seed_policies()

    def _seed_policies(self) -> None:
        seeds = (
            (DOC_TERMS, "1.0-draft", "Terms of Service (draft — legal review required)"),
            (DOC_PRIVACY, "1.0-draft", "Privacy Policy (draft — legal review required)"),
            (DOC_PERSONAL_DATA, "1.0-draft", "Personal Data Policy (draft — legal review required)"),
            (DOC_CONSENT_TEXT, "1.0-draft", "Consent text templates (draft — legal review required)"),
            (DOC_AI_DISCLOSURE, "1.0-draft", "AI product disclosure (draft — legal review required)"),
        )
        for doc_type, version, title in seeds:
            if self.store.get_policy(doc_type, version) is None:
                self.store.upsert_policy(
                    PolicyDocument(
                        document_id=str(uuid.uuid4()),
                        document_type=doc_type,
                        version=version,
                        effective_from=_iso(),
                        status="CURRENT",
                        content_reference=f"/legal/{doc_type.lower()}/{version}",
                        title=title,
                        created_at=_iso(),
                        draft_requires_legal_review=True,
                    )
                )

    def inventory(self) -> list[dict]:
        return [e.__dict__ for e in DATA_INVENTORY]

    def retention_policy(self) -> list[dict]:
        return [r.__dict__ for r in RETENTION_CLASSES]

    def cookie_inventory(self) -> list[dict]:
        return [c.__dict__ for c in COOKIE_INVENTORY]

    def processor_inventory(self) -> list[dict]:
        return [
            {
                "service_class": p.service_class,
                "data_categories": list(p.data_categories),
                "purpose": p.purpose,
                "destination_region": p.destination_region,
                "transfer_allowed_policy": p.transfer_allowed_policy,
                "minimization_rule": p.minimization_rule,
            }
            for p in PROCESSOR_INVENTORY
        ]

    def age_policy_status(self) -> str:
        return AGE_POLICY_STATUS

    def publish_policy_version(self, *, document_type: str, version: str, title: str, content_reference: str) -> PolicyDocument:
        # Mark previous CURRENT as SUPERSEDED without deleting historical evidence
        current = self.store.get_current_policy(document_type)
        if current is not None:
            self.store.upsert_policy(
                PolicyDocument(**{**current.__dict__, "status": "SUPERSEDED"})
            )
        doc = PolicyDocument(
            document_id=str(uuid.uuid4()),
            document_type=document_type,
            version=version,
            effective_from=_iso(),
            status="CURRENT",
            content_reference=content_reference,
            title=title,
            created_at=_iso(),
            draft_requires_legal_review=True,
        )
        return self.store.upsert_policy(doc)

    def record_decision(
        self,
        *,
        user_id: str,
        tenant_id: str,
        decision_type: str,
        decision: str,
        source: str,
        document_type: str = "",
        document_version: str = "",
    ) -> UserDecisionRecord:
        if decision_type in {DEC_TERMS, DEC_PRIVACY_ACK, DEC_PERSONAL_DATA} and not document_version:
            current = self.store.get_current_policy(document_type or self._doc_for_decision(decision_type))
            if current is None:
                raise ValueError("policy_missing")
            document_type = current.document_type
            document_version = current.version
        rec = UserDecisionRecord(
            decision_id=str(uuid.uuid4()),
            user_id=user_id,
            tenant_id=tenant_id,
            document_type=document_type,
            document_version=document_version,
            decision_type=decision_type,
            decision=decision,
            timestamp=_iso(),
            source=source,
        )
        self.store.record_decision(rec)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(),
                actor_id=user_id,
                target_id=rec.decision_id,
                tenant_id=tenant_id,
                action="consent.recorded",
                result="ok",
                metadata={"decision_type": decision_type, "document_version": document_version},
            )
        )
        return rec

    def withdraw_decision(self, *, user_id: str, decision_type: str) -> UserDecisionRecord | None:
        decisions = self.store.list_decisions(user_id=user_id, decision_type=decision_type)
        active = [d for d in decisions if d.decision == "ACCEPTED" and not d.withdrawn_at]
        if not active:
            return None
        last = active[-1]
        updated = UserDecisionRecord(
            **{**last.__dict__, "decision": "WITHDRAWN", "withdrawn_at": _iso()}
        )
        self.store.update_decision(updated)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(),
                actor_id=user_id,
                target_id=updated.decision_id,
                tenant_id=updated.tenant_id,
                action="consent.withdrawn",
                result="ok",
                metadata={"decision_type": decision_type},
            )
        )
        return updated

    def marketing_eligible(self, user_id: str) -> bool:
        decisions = self.store.list_decisions(user_id=user_id, decision_type=DEC_MARKETING)
        if not decisions:
            return False
        last = decisions[-1]
        return last.decision == "ACCEPTED" and not last.withdrawn_at

    def export_account_data(self, *, user_id: str, tenant_id: str) -> dict:
        user = self.store.get_user(user_id)
        if user is None or user.tenant_id != tenant_id:
            raise PermissionError("tenant_scope")
        decisions = self.store.list_decisions(user_id=user_id)
        return {
            "user": {
                "user_id": user.user_id,
                "username": user.username,
                "display_name": user.display_name,
                "email": user.email,
                "role": user.role,
                "status": user.status,
                "created_at": user.created_at,
                "last_login_at": user.last_login_at,
            },
            "decisions": [
                {
                    "decision_type": d.decision_type,
                    "decision": d.decision,
                    "document_type": d.document_type,
                    "document_version": d.document_version,
                    "timestamp": d.timestamp,
                    "withdrawn_at": d.withdrawn_at,
                }
                for d in decisions
            ],
            "excluded": ["password_hash", "session_secrets", "api_keys", "payment_credentials"],
        }

    def request_deletion(self, *, user_id: str, tenant_id: str, actor_id: str) -> DeletionRequestRecord:
        if actor_id != user_id:
            raise PermissionError("self_only")
        user = self.store.get_user(user_id)
        if user is None or user.tenant_id != tenant_id:
            raise PermissionError("tenant_scope")
        # Idempotent: return existing open request
        # Simple scan
        existing = None
        # store doesn't list by user — create new with same logical path via audit
        req = DeletionRequestRecord(
            request_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            status="REQUESTED",
            created_at=_iso(),
            updated_at=_iso(),
            retention_hold=True,  # billing/audit retention may block blind wipe
        )
        self.store.create_deletion_request(req)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(),
                actor_id=actor_id,
                target_id=user_id,
                tenant_id=tenant_id,
                action="account.deletion_requested",
                result="ok",
            )
        )
        return req

    def advance_deletion(self, request_id: str) -> DeletionRequestRecord:
        req = self.store.get_deletion_request(request_id)
        if req is None:
            raise KeyError("not_found")
        if req.status == "COMPLETED":
            return req
        # Lifecycle: REQUESTED → VALIDATED → RETENTION_CHECK → DELETE_OR_ANONYMIZE → COMPLETED
        transitions = {
            "REQUESTED": "VALIDATED",
            "VALIDATED": "RETENTION_CHECK",
            "RETENTION_CHECK": "ANONYMIZED" if req.retention_hold else "DELETED",
            "ANONYMIZED": "COMPLETED",
            "DELETED": "COMPLETED",
        }
        nxt = transitions.get(req.status, req.status)
        if nxt in {"ANONYMIZED", "DELETED"}:
            user = self.store.get_user(req.user_id)
            if user is not None:
                from accounts.models import STATUS_DISABLED
                from accounts.passwords import hash_password

                # Disable + rotate password hash to random (no plaintext)
                import secrets

                from accounts.identity_service import IdentityService

                # Direct hash without exposing temp password beyond this scope
                from accounts.passwords import hash_password as hp

                updated = user.__dict__.copy()
                updated["status"] = STATUS_DISABLED
                updated["password_hash"] = hp(secrets.token_urlsafe(24) + "Aa1!")
                updated["display_name"] = "deleted-user"
                updated["email"] = ""
                updated["updated_at"] = _iso()
                from accounts.models import HumanUserRecord

                self.store.update_user(HumanUserRecord(**updated))
                self.store.revoke_sessions_for_user(req.user_id, now=_iso())
            nxt = "COMPLETED"
        updated = DeletionRequestRecord(
            **{
                **req.__dict__,
                "status": nxt,
                "updated_at": _iso(),
                "completed_at": _iso() if nxt == "COMPLETED" else req.completed_at,
            }
        )
        return self.store.update_deletion_request(updated)

    @staticmethod
    def _doc_for_decision(decision_type: str) -> str:
        return {
            DEC_TERMS: DOC_TERMS,
            DEC_PRIVACY_ACK: DOC_PRIVACY,
            DEC_PERSONAL_DATA: DOC_PERSONAL_DATA,
            DEC_MARKETING: DOC_CONSENT_TEXT,
            DEC_OPTIONAL_ANALYTICS: DOC_CONSENT_TEXT,
        }.get(decision_type, DOC_CONSENT_TEXT)

    def ai_disclosure(self) -> dict:
        doc = self.store.get_current_policy(DOC_AI_DISCLOSURE)
        return {
            "document_type": DOC_AI_DISCLOSURE,
            "version": doc.version if doc else "1.0-draft",
            "draft_requires_legal_review": True,
            "statements": [
                "Panda is an AI-based assistant.",
                "AI-generated information may contain errors.",
                "Important or consequential information should be independently verified.",
                "Panda does not automatically replace qualified professional advice.",
            ],
        }
