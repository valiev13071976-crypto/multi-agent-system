"""SQLite persistence for SaaS product domain."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from saas_product.capabilities import ROLE_OWNER
from saas_product.models import (
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_REMOVED,
    BillingEventRecord,
    DeletionJob,
    InvitationRecord,
    InvoiceRecord,
    MembershipRecord,
    PrivacyExportJob,
    ProductAuditEvent,
    SubscriptionRecord,
    TenantRecord,
    UsageMeterRecord,
    UserAccount,
)
from security.redaction import redact


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteSaaSProductStore:
    SCHEMA_VERSION = 1

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS saas_schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS saas_users (
                user_id TEXT PRIMARY KEY, status TEXT NOT NULL, display_name TEXT,
                email TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saas_tenants (
                tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                owner_user_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saas_memberships (
                membership_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL,
                UNIQUE(tenant_id, user_id, status)
            );
            CREATE INDEX IF NOT EXISTS idx_saas_memberships_tenant ON saas_memberships(tenant_id, status);
            CREATE INDEX IF NOT EXISTS idx_saas_memberships_user ON saas_memberships(user_id, status);
            CREATE TABLE IF NOT EXISTS saas_invitations (
                invitation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL,
                role TEXT NOT NULL, invited_by TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_saas_invitations_tenant ON saas_invitations(tenant_id, status);
            CREATE TABLE IF NOT EXISTS saas_subscriptions (
                subscription_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
                provider_customer_ref TEXT, provider_subscription_ref TEXT,
                plan_id TEXT NOT NULL, plan_version TEXT NOT NULL, status TEXT NOT NULL,
                current_period_start TEXT, current_period_end TEXT, cancel_at_period_end INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saas_invoices (
                invoice_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
                provider_invoice_ref TEXT, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
                status TEXT NOT NULL, period_start TEXT, period_end TEXT,
                created_at TEXT NOT NULL, paid_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_saas_invoices_tenant ON saas_invoices(tenant_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS saas_billing_events (
                event_id TEXT PRIMARY KEY, provider TEXT NOT NULL, event_type TEXT NOT NULL,
                tenant_id TEXT NOT NULL, subscription_id TEXT, payload_hash TEXT NOT NULL,
                processed_at TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saas_usage_meters (
                idempotency_key TEXT PRIMARY KEY, meter_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL, quantity INTEGER NOT NULL, period_key TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_saas_usage_tenant ON saas_usage_meters(tenant_id, meter_id, period_key);
            CREATE TABLE IF NOT EXISTS saas_privacy_exports (
                job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                status TEXT NOT NULL, artifact_ref TEXT, manifest_version TEXT NOT NULL,
                created_at TEXT NOT NULL, completed_at TEXT, error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS saas_deletion_jobs (
                job_id TEXT PRIMARY KEY, scope TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                status TEXT NOT NULL, confirmation_token_hash TEXT, phases_completed TEXT NOT NULL,
                created_at TEXT NOT NULL, completed_at TEXT, error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS saas_product_audit (
                event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, actor_ref TEXT NOT NULL,
                tenant_id TEXT NOT NULL, action TEXT NOT NULL, target_type TEXT NOT NULL,
                target_id TEXT NOT NULL, result TEXT NOT NULL, reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_saas_audit_tenant ON saas_product_audit(tenant_id, timestamp DESC);
            """
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO saas_schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(self.SCHEMA_VERSION),),
        )
        self._conn.commit()

    def create_user(self, user: UserAccount) -> UserAccount:
        self._conn.execute(
            "INSERT INTO saas_users (user_id, status, display_name, email, created_at, updated_at, version) VALUES (?,?,?,?,?,?,?)",
            (user.user_id, user.status, user.display_name, user.email, user.created_at or _utc(), user.updated_at or _utc(), user.version),
        )
        self._conn.commit()
        return user

    def get_user(self, user_id: str) -> UserAccount | None:
        row = self._conn.execute("SELECT * FROM saas_users WHERE user_id=?", (user_id,)).fetchone()
        return self._user(row) if row else None

    def update_user(self, user: UserAccount) -> UserAccount:
        self._conn.execute(
            "UPDATE saas_users SET status=?, display_name=?, email=?, updated_at=?, version=? WHERE user_id=?",
            (user.status, user.display_name, user.email, user.updated_at or _utc(), user.version, user.user_id),
        )
        self._conn.commit()
        return user

    def create_tenant(self, tenant: TenantRecord) -> TenantRecord:
        self._conn.execute(
            "INSERT INTO saas_tenants (tenant_id, name, status, owner_user_id, created_at, updated_at, version) VALUES (?,?,?,?,?,?,?)",
            (tenant.tenant_id, tenant.name, tenant.status, tenant.owner_user_id, tenant.created_at or _utc(), tenant.updated_at or _utc(), tenant.version),
        )
        self._conn.commit()
        return tenant

    def get_tenant(self, tenant_id: str) -> TenantRecord | None:
        row = self._conn.execute("SELECT * FROM saas_tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        return self._tenant(row) if row else None

    def update_tenant(self, tenant: TenantRecord) -> TenantRecord:
        self._conn.execute(
            "UPDATE saas_tenants SET name=?, status=?, owner_user_id=?, updated_at=?, version=? WHERE tenant_id=?",
            (tenant.name, tenant.status, tenant.owner_user_id, tenant.updated_at or _utc(), tenant.version, tenant.tenant_id),
        )
        self._conn.commit()
        return tenant

    def list_tenants_for_user(self, user_id: str) -> tuple[TenantRecord, ...]:
        rows = self._conn.execute(
            """
            SELECT t.* FROM saas_tenants t
            JOIN saas_memberships m ON m.tenant_id = t.tenant_id
            WHERE m.user_id=? AND m.status=? AND t.status != 'DELETED'
            ORDER BY t.created_at DESC
            """,
            (user_id, MEMBERSHIP_ACTIVE),
        ).fetchall()
        return tuple(self._tenant(r) for r in rows)

    def create_membership(self, membership: MembershipRecord) -> MembershipRecord:
        self._conn.execute(
            "INSERT INTO saas_memberships (membership_id, tenant_id, user_id, role, status, created_at, version) VALUES (?,?,?,?,?,?,?)",
            (membership.membership_id, membership.tenant_id, membership.user_id, membership.role, membership.status, membership.created_at or _utc(), membership.version),
        )
        self._conn.commit()
        return membership

    def get_membership(self, membership_id: str) -> MembershipRecord | None:
        row = self._conn.execute("SELECT * FROM saas_memberships WHERE membership_id=?", (membership_id,)).fetchone()
        return self._membership(row) if row else None

    def get_active_membership(self, user_id: str, tenant_id: str) -> MembershipRecord | None:
        row = self._conn.execute(
            "SELECT * FROM saas_memberships WHERE user_id=? AND tenant_id=? AND status=?",
            (user_id, tenant_id, MEMBERSHIP_ACTIVE),
        ).fetchone()
        return self._membership(row) if row else None

    def list_memberships(self, tenant_id: str, *, limit: int = 50, offset: int = 0) -> tuple[list[MembershipRecord], int]:
        limit = min(max(1, limit), 200)
        total = self._conn.execute(
            "SELECT COUNT(*) FROM saas_memberships WHERE tenant_id=? AND status=?",
            (tenant_id, MEMBERSHIP_ACTIVE),
        ).fetchone()[0]
        rows = self._conn.execute(
            "SELECT * FROM saas_memberships WHERE tenant_id=? AND status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (tenant_id, MEMBERSHIP_ACTIVE, limit, max(0, offset)),
        ).fetchall()
        return [self._membership(r) for r in rows], int(total)

    def update_membership(self, membership: MembershipRecord) -> MembershipRecord:
        self._conn.execute(
            "UPDATE saas_memberships SET role=?, status=?, version=? WHERE membership_id=? AND version=?",
            (membership.role, membership.status, membership.version + 1, membership.membership_id, membership.version),
        )
        if self._conn.total_changes == 0:
            raise ValueError("stale_membership")
        self._conn.commit()
        return MembershipRecord(**{**membership.__dict__, "version": membership.version + 1})

    def deactivate_memberships_for_user(self, user_id: str, *, tenant_id: str | None = None) -> int:
        if tenant_id:
            cur = self._conn.execute(
                "UPDATE saas_memberships SET status=? WHERE user_id=? AND tenant_id=? AND status=?",
                (MEMBERSHIP_REMOVED, user_id, tenant_id, MEMBERSHIP_ACTIVE),
            )
        else:
            cur = self._conn.execute(
                "UPDATE saas_memberships SET status=? WHERE user_id=? AND status=?",
                (MEMBERSHIP_REMOVED, user_id, MEMBERSHIP_ACTIVE),
            )
        self._conn.commit()
        return int(cur.rowcount)

    def deactivate_memberships_for_tenant(self, tenant_id: str) -> int:
        cur = self._conn.execute(
            "UPDATE saas_memberships SET status=? WHERE tenant_id=? AND status=?",
            (MEMBERSHIP_REMOVED, tenant_id, MEMBERSHIP_ACTIVE),
        )
        self._conn.commit()
        return int(cur.rowcount)

    def create_invitation(self, invitation: InvitationRecord) -> InvitationRecord:
        self._conn.execute(
            """INSERT INTO saas_invitations
            (invitation_id, tenant_id, email, role, invited_by, token_hash, status, expires_at, created_at, version)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (invitation.invitation_id, invitation.tenant_id, invitation.email, invitation.role, invitation.invited_by,
             invitation.token_hash, invitation.status, invitation.expires_at, invitation.created_at or _utc(), invitation.version),
        )
        self._conn.commit()
        return invitation

    def get_invitation(self, invitation_id: str) -> InvitationRecord | None:
        row = self._conn.execute("SELECT * FROM saas_invitations WHERE invitation_id=?", (invitation_id,)).fetchone()
        return self._invitation(row) if row else None

    def get_invitation_by_token_hash(self, token_hash: str) -> InvitationRecord | None:
        row = self._conn.execute("SELECT * FROM saas_invitations WHERE token_hash=?", (token_hash,)).fetchone()
        return self._invitation(row) if row else None

    def list_invitations(self, tenant_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[InvitationRecord], int]:
        limit = min(max(1, limit), 200)
        if status:
            total = self._conn.execute("SELECT COUNT(*) FROM saas_invitations WHERE tenant_id=? AND status=?", (tenant_id, status)).fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM saas_invitations WHERE tenant_id=? AND status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, status, limit, max(0, offset)),
            ).fetchall()
        else:
            total = self._conn.execute("SELECT COUNT(*) FROM saas_invitations WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM saas_invitations WHERE tenant_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, max(0, offset)),
            ).fetchall()
        return [self._invitation(r) for r in rows], int(total)

    def update_invitation(self, invitation: InvitationRecord) -> InvitationRecord:
        self._conn.execute(
            "UPDATE saas_invitations SET status=?, version=? WHERE invitation_id=? AND version=?",
            (invitation.status, invitation.version + 1, invitation.invitation_id, invitation.version),
        )
        self._conn.commit()
        return InvitationRecord(**{**invitation.__dict__, "version": invitation.version + 1})

    def save_subscription(self, subscription: SubscriptionRecord) -> SubscriptionRecord:
        self._conn.execute(
            """INSERT OR REPLACE INTO saas_subscriptions
            (subscription_id, tenant_id, provider, provider_customer_ref, provider_subscription_ref,
             plan_id, plan_version, status, current_period_start, current_period_end, cancel_at_period_end,
             created_at, updated_at, version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (subscription.subscription_id, subscription.tenant_id, subscription.provider,
             subscription.provider_customer_ref, subscription.provider_subscription_ref,
             subscription.plan_id, subscription.plan_version, subscription.status,
             subscription.current_period_start, subscription.current_period_end,
             1 if subscription.cancel_at_period_end else 0,
             subscription.created_at or _utc(), subscription.updated_at or _utc(), subscription.version),
        )
        self._conn.commit()
        return subscription

    def get_subscription(self, subscription_id: str) -> SubscriptionRecord | None:
        row = self._conn.execute("SELECT * FROM saas_subscriptions WHERE subscription_id=?", (subscription_id,)).fetchone()
        return self._subscription(row) if row else None

    def get_subscription_for_tenant(self, tenant_id: str) -> SubscriptionRecord | None:
        row = self._conn.execute("SELECT * FROM saas_subscriptions WHERE tenant_id=?", (tenant_id,)).fetchone()
        return self._subscription(row) if row else None

    def save_invoice(self, invoice: InvoiceRecord) -> InvoiceRecord:
        self._conn.execute(
            """INSERT OR REPLACE INTO saas_invoices
            (invoice_id, tenant_id, subscription_id, provider_invoice_ref, amount_minor, currency,
             status, period_start, period_end, created_at, paid_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (invoice.invoice_id, invoice.tenant_id, invoice.subscription_id, invoice.provider_invoice_ref,
             invoice.amount_minor, invoice.currency, invoice.status, invoice.period_start, invoice.period_end,
             invoice.created_at or _utc(), invoice.paid_at),
        )
        self._conn.commit()
        return invoice

    def list_invoices(self, tenant_id: str, *, limit: int = 50, offset: int = 0) -> tuple[list[InvoiceRecord], int]:
        limit = min(max(1, limit), 200)
        total = self._conn.execute("SELECT COUNT(*) FROM saas_invoices WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
        rows = self._conn.execute(
            "SELECT * FROM saas_invoices WHERE tenant_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, max(0, offset)),
        ).fetchall()
        return [self._invoice(r) for r in rows], int(total)

    def record_billing_event(self, event: BillingEventRecord) -> BillingEventRecord:
        self._conn.execute(
            "INSERT INTO saas_billing_events (event_id, provider, event_type, tenant_id, subscription_id, payload_hash, processed_at, result) VALUES (?,?,?,?,?,?,?,?)",
            (event.event_id, event.provider, event.event_type, event.tenant_id, event.subscription_id, event.payload_hash, event.processed_at, event.result),
        )
        self._conn.commit()
        return event

    def get_billing_event(self, event_id: str) -> BillingEventRecord | None:
        row = self._conn.execute("SELECT * FROM saas_billing_events WHERE event_id=?", (event_id,)).fetchone()
        return self._billing_event(row) if row else None

    def record_usage_meter(self, record: UsageMeterRecord) -> UsageMeterRecord:
        self._conn.execute(
            "INSERT OR IGNORE INTO saas_usage_meters (idempotency_key, meter_id, tenant_id, user_id, quantity, period_key, created_at) VALUES (?,?,?,?,?,?,?)",
            (record.idempotency_key, record.meter_id, record.tenant_id, record.user_id, record.quantity, record.period_key, record.created_at or _utc()),
        )
        self._conn.commit()
        return record

    def sum_usage_meter(self, tenant_id: str, meter_id: str, period_key: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM saas_usage_meters WHERE tenant_id=? AND meter_id=? AND period_key=?",
            (tenant_id, meter_id, period_key),
        ).fetchone()
        return int(row[0])

    def try_reserve_usage(
        self,
        *,
        tenant_id: str,
        meter_id: str,
        period_key: str,
        limit: int,
        idempotency_key: str,
        user_id: str,
        quantity: int = 1,
    ) -> bool:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._conn.execute(
                "SELECT 1 FROM saas_usage_meters WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                self._conn.commit()
                return True
            row = self._conn.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM saas_usage_meters WHERE tenant_id=? AND meter_id=? AND period_key=?",
                (tenant_id, meter_id, period_key),
            ).fetchone()
            used = int(row[0])
            if used + quantity > limit:
                self._conn.rollback()
                return False
            self._conn.execute(
                "INSERT INTO saas_usage_meters (idempotency_key, meter_id, tenant_id, user_id, quantity, period_key, created_at) VALUES (?,?,?,?,?,?,?)",
                (idempotency_key, meter_id, tenant_id, user_id, quantity, period_key, _utc()),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def create_privacy_export(self, job: PrivacyExportJob) -> PrivacyExportJob:
        self._conn.execute(
            "INSERT INTO saas_privacy_exports (job_id, tenant_id, user_id, status, artifact_ref, manifest_version, created_at, completed_at, error_code) VALUES (?,?,?,?,?,?,?,?,?)",
            (job.job_id, job.tenant_id, job.user_id, job.status, job.artifact_ref, job.manifest_version, job.created_at or _utc(), job.completed_at, job.error_code),
        )
        self._conn.commit()
        return job

    def get_privacy_export(self, job_id: str) -> PrivacyExportJob | None:
        row = self._conn.execute("SELECT * FROM saas_privacy_exports WHERE job_id=?", (job_id,)).fetchone()
        return self._privacy_export(row) if row else None

    def update_privacy_export(self, job: PrivacyExportJob) -> PrivacyExportJob:
        self._conn.execute(
            "UPDATE saas_privacy_exports SET status=?, artifact_ref=?, completed_at=?, error_code=? WHERE job_id=?",
            (job.status, job.artifact_ref, job.completed_at, job.error_code, job.job_id),
        )
        self._conn.commit()
        return job

    def list_privacy_exports(self, tenant_id: str, user_id: str, *, limit: int = 50, offset: int = 0) -> tuple[list[PrivacyExportJob], int]:
        limit = min(max(1, limit), 200)
        total = self._conn.execute(
            "SELECT COUNT(*) FROM saas_privacy_exports WHERE tenant_id=? AND user_id=?",
            (tenant_id, user_id),
        ).fetchone()[0]
        rows = self._conn.execute(
            "SELECT * FROM saas_privacy_exports WHERE tenant_id=? AND user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (tenant_id, user_id, limit, max(0, offset)),
        ).fetchall()
        return [self._privacy_export(r) for r in rows], int(total)

    def create_deletion_job(self, job: DeletionJob) -> DeletionJob:
        self._conn.execute(
            "INSERT INTO saas_deletion_jobs (job_id, scope, tenant_id, user_id, status, confirmation_token_hash, phases_completed, created_at, completed_at, error_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job.job_id, job.scope, job.tenant_id, job.user_id, job.status, job.confirmation_token_hash,
             json.dumps(list(job.phases_completed)), job.created_at or _utc(), job.completed_at, job.error_code),
        )
        self._conn.commit()
        return job

    def get_deletion_job(self, job_id: str) -> DeletionJob | None:
        row = self._conn.execute("SELECT * FROM saas_deletion_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._deletion_job(row) if row else None

    def update_deletion_job(self, job: DeletionJob) -> DeletionJob:
        self._conn.execute(
            "UPDATE saas_deletion_jobs SET status=?, phases_completed=?, completed_at=?, error_code=? WHERE job_id=?",
            (job.status, json.dumps(list(job.phases_completed)), job.completed_at, job.error_code, job.job_id),
        )
        self._conn.commit()
        return job

    def append_audit(self, event: ProductAuditEvent) -> ProductAuditEvent:
        self._conn.execute(
            "INSERT INTO saas_product_audit (event_id, timestamp, actor_ref, tenant_id, action, target_type, target_id, result, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (event.event_id, event.timestamp, event.actor_ref, event.tenant_id, event.action, event.target_type, event.target_id, event.result, redact(event.reason or "")[:500] if event.reason else None),
        )
        self._conn.commit()
        return event

    def list_audit(self, tenant_id: str | None, *, limit: int = 50, offset: int = 0) -> tuple[list[ProductAuditEvent], int]:
        limit = min(max(1, limit), 200)
        if tenant_id:
            total = self._conn.execute("SELECT COUNT(*) FROM saas_product_audit WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM saas_product_audit WHERE tenant_id=? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, max(0, offset)),
            ).fetchall()
        else:
            total = self._conn.execute("SELECT COUNT(*) FROM saas_product_audit").fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM saas_product_audit ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, max(0, offset)),
            ).fetchall()
        return [self._audit(r) for r in rows], int(total)

    def count_active_owners(self, tenant_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM saas_memberships WHERE tenant_id=? AND role=? AND status=?",
            (tenant_id, ROLE_OWNER, MEMBERSHIP_ACTIVE),
        ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _user(row) -> UserAccount:
        return UserAccount(user_id=row["user_id"], status=row["status"], display_name=row["display_name"] or "", email=row["email"] or "", created_at=row["created_at"], updated_at=row["updated_at"], version=int(row["version"]))

    @staticmethod
    def _tenant(row) -> TenantRecord:
        return TenantRecord(tenant_id=row["tenant_id"], name=row["name"], status=row["status"], owner_user_id=row["owner_user_id"], created_at=row["created_at"], updated_at=row["updated_at"], version=int(row["version"]))

    @staticmethod
    def _membership(row) -> MembershipRecord:
        return MembershipRecord(membership_id=row["membership_id"], tenant_id=row["tenant_id"], user_id=row["user_id"], role=row["role"], status=row["status"], created_at=row["created_at"], version=int(row["version"]))

    @staticmethod
    def _invitation(row) -> InvitationRecord:
        return InvitationRecord(invitation_id=row["invitation_id"], tenant_id=row["tenant_id"], email=row["email"], role=row["role"], invited_by=row["invited_by"], token_hash=row["token_hash"], status=row["status"], expires_at=row["expires_at"], created_at=row["created_at"], version=int(row["version"]))

    @staticmethod
    def _subscription(row) -> SubscriptionRecord:
        return SubscriptionRecord(
            subscription_id=row["subscription_id"], tenant_id=row["tenant_id"], provider=row["provider"],
            provider_customer_ref=row["provider_customer_ref"] or "", provider_subscription_ref=row["provider_subscription_ref"] or "",
            plan_id=row["plan_id"], plan_version=row["plan_version"], status=row["status"],
            current_period_start=row["current_period_start"] or "", current_period_end=row["current_period_end"] or "",
            cancel_at_period_end=bool(row["cancel_at_period_end"]), created_at=row["created_at"], updated_at=row["updated_at"], version=int(row["version"]),
        )

    @staticmethod
    def _invoice(row) -> InvoiceRecord:
        return InvoiceRecord(
            invoice_id=row["invoice_id"], tenant_id=row["tenant_id"], subscription_id=row["subscription_id"],
            provider_invoice_ref=row["provider_invoice_ref"] or "", amount_minor=int(row["amount_minor"]),
            currency=row["currency"], status=row["status"], period_start=row["period_start"] or "",
            period_end=row["period_end"] or "", created_at=row["created_at"], paid_at=row["paid_at"],
        )

    @staticmethod
    def _billing_event(row) -> BillingEventRecord:
        return BillingEventRecord(
            event_id=row["event_id"], provider=row["provider"], event_type=row["event_type"],
            tenant_id=row["tenant_id"], subscription_id=row["subscription_id"] or "",
            payload_hash=row["payload_hash"], processed_at=row["processed_at"], result=row["result"],
        )

    @staticmethod
    def _privacy_export(row) -> PrivacyExportJob:
        return PrivacyExportJob(
            job_id=row["job_id"], tenant_id=row["tenant_id"], user_id=row["user_id"], status=row["status"],
            artifact_ref=row["artifact_ref"] or "", manifest_version=row["manifest_version"],
            created_at=row["created_at"], completed_at=row["completed_at"], error_code=row["error_code"],
        )

    @staticmethod
    def _deletion_job(row) -> DeletionJob:
        phases = tuple(json.loads(row["phases_completed"] or "[]"))
        return DeletionJob(
            job_id=row["job_id"], scope=row["scope"], tenant_id=row["tenant_id"], user_id=row["user_id"],
            status=row["status"], confirmation_token_hash=row["confirmation_token_hash"] or "",
            phases_completed=phases, created_at=row["created_at"], completed_at=row["completed_at"], error_code=row["error_code"],
        )

    @staticmethod
    def _audit(row) -> ProductAuditEvent:
        return ProductAuditEvent(
            event_id=row["event_id"], timestamp=row["timestamp"], actor_ref=row["actor_ref"],
            tenant_id=row["tenant_id"], action=row["action"], target_type=row["target_type"],
            target_id=row["target_id"], result=row["result"], reason=row["reason"],
        )
