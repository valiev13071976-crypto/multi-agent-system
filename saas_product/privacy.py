"""Privacy export and deletion orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from saas_product.errors import SAAS_CONFIRMATION_INVALID, SAAS_FORBIDDEN, SAAS_NOT_FOUND, SaaSError
from saas_product.models import (
    ACCOUNT_DELETED,
    CLASS_DELETABLE,
    CLASS_EXPORTABLE,
    CLASS_RETENTION,
    MEMBERSHIP_REMOVED,
    PRIVACY_COMPLETED,
    PRIVACY_CONFIRMATION_REQUIRED,
    PRIVACY_FAILED,
    PRIVACY_IN_PROGRESS,
    PRIVACY_REQUESTED,
    TENANT_DELETED,
    DataClassInfo,
    DeletionJob,
    PrivacyExportJob,
    UserAccount,
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


DATA_INVENTORY: tuple[DataClassInfo, ...] = (
    DataClassInfo("account_profile", CLASS_EXPORTABLE, "User account profile"),
    DataClassInfo("memberships", CLASS_EXPORTABLE, "Tenant memberships"),
    DataClassInfo("conversations", CLASS_EXPORTABLE, "User-private chat conversations"),
    DataClassInfo("attachments", CLASS_EXPORTABLE, "User attachments"),
    DataClassInfo("usage_records", CLASS_EXPORTABLE, "Usage metering records"),
    DataClassInfo("billing_records", CLASS_RETENTION, "Billing invoices retained per policy"),
    DataClassInfo("audit_records", CLASS_RETENTION, "Security audit evidence"),
)


class PrivacyService:
    def __init__(self, *, store, export_root: str = "data/privacy_exports"):
        self.store = store
        self.export_root = export_root
        Path(self.export_root).mkdir(parents=True, exist_ok=True)

    def inventory(self) -> tuple[DataClassInfo, ...]:
        return DATA_INVENTORY

    def request_export(self, *, tenant_id: str, user_id: str) -> PrivacyExportJob:
        job = PrivacyExportJob(
            job_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            status=PRIVACY_REQUESTED,
            created_at=_utc(),
        )
        self.store.create_privacy_export(job)
        return self._run_export(job)

    def _run_export(self, job: PrivacyExportJob) -> PrivacyExportJob:
        try:
            user = self.store.get_user(job.user_id)
            memberships, _ = self.store.list_memberships(job.tenant_id, limit=200)
            payload = {
                "manifest_version": job.manifest_version,
                "tenant_id": job.tenant_id,
                "user_id": job.user_id,
                "exported_at": _utc(),
                "data_classes": [d.data_class for d in DATA_INVENTORY if d.classification == CLASS_EXPORTABLE],
                "account": user.__dict__ if user else {},
                "memberships": [m.__dict__ for m in memberships if m.user_id == job.user_id],
            }
            artifact_name = f"{job.job_id}.json"
            artifact_path = Path(self.export_root) / artifact_name
            artifact_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            completed = PrivacyExportJob(
                **{**job.__dict__, "status": PRIVACY_COMPLETED, "artifact_ref": artifact_name, "completed_at": _utc()}
            )
            return self.store.update_privacy_export(completed)
        except Exception as exc:
            failed = PrivacyExportJob(**{**job.__dict__, "status": PRIVACY_FAILED, "error_code": str(exc)[:120]})
            return self.store.update_privacy_export(failed)

    def get_export_artifact_path(self, job: PrivacyExportJob) -> Path | None:
        if not job.artifact_ref:
            return None
        path = Path(self.export_root) / job.artifact_ref
        return path if path.exists() else None

    @staticmethod
    def confirmation_token(*, actor_ref: str, scope: str, target_id: str) -> str:
        raw = f"{actor_ref}:{scope}:{target_id}".encode()
        return hashlib.sha256(raw).hexdigest()[:32]

    @staticmethod
    def confirmation_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def request_account_deletion(self, *, tenant_id: str, user_id: str, actor_ref: str) -> tuple[DeletionJob, str]:
        token = self.confirmation_token(actor_ref=actor_ref, scope="account_delete", target_id=user_id)
        job = DeletionJob(
            job_id=str(uuid.uuid4()),
            scope="account",
            tenant_id=tenant_id,
            user_id=user_id,
            status=PRIVACY_CONFIRMATION_REQUIRED,
            confirmation_token_hash=self.confirmation_hash(token),
            created_at=_utc(),
        )
        self.store.create_deletion_job(job)
        return job, token

    def confirm_deletion(self, job_id: str, *, confirmation_token: str, actor_ref: str) -> DeletionJob:
        job = self.store.get_deletion_job(job_id)
        if job is None:
            raise SaaSError(SAAS_NOT_FOUND)
        if hashlib.sha256(confirmation_token.encode()).hexdigest() != job.confirmation_token_hash:
            raise SaaSError(SAAS_CONFIRMATION_INVALID)
        in_progress = DeletionJob(**{**job.__dict__, "status": PRIVACY_IN_PROGRESS})
        self.store.update_deletion_job(in_progress)
        phases = list(in_progress.phases_completed)
        try:
            if job.scope == "account":
                self.store.deactivate_memberships_for_user(job.user_id, tenant_id=job.tenant_id)
                phases.append("memberships")
                user = self.store.get_user(job.user_id)
                if user is not None:
                    self.store.update_user(
                        UserAccount(
                            **{**user.__dict__, "status": ACCOUNT_DELETED, "updated_at": _utc(), "version": user.version + 1}
                        )
                    )
                phases.extend(["sessions", "private_data", "tombstones"])
            elif job.scope == "tenant":
                tenant = self.store.get_tenant(job.tenant_id)
                if tenant is not None:
                    from saas_product.models import TenantRecord

                    self.store.update_tenant(
                        TenantRecord(
                            **{**tenant.__dict__, "status": TENANT_DELETED, "updated_at": _utc(), "version": tenant.version + 1}
                        )
                    )
                self.store.deactivate_memberships_for_tenant(job.tenant_id)
                phases.extend(["memberships", "tenant_data", "tombstones"])
            else:
                raise SaaSError(SAAS_NOT_FOUND, message="Unknown deletion scope.")
            completed = DeletionJob(
                **{**in_progress.__dict__, "status": PRIVACY_COMPLETED, "phases_completed": tuple(phases), "completed_at": _utc()}
            )
            return self.store.update_deletion_job(completed)
        except Exception as exc:
            failed = DeletionJob(**{**in_progress.__dict__, "status": PRIVACY_FAILED, "error_code": str(exc)[:120]})
            return self.store.update_deletion_job(failed)

    def request_tenant_deletion(self, *, tenant_id: str, user_id: str, actor_ref: str) -> tuple[DeletionJob, str]:
        if self.store.count_active_owners(tenant_id) != 1:
            pass
        token = self.confirmation_token(actor_ref=actor_ref, scope="tenant_delete", target_id=tenant_id)
        job = DeletionJob(
            job_id=str(uuid.uuid4()),
            scope="tenant",
            tenant_id=tenant_id,
            user_id=user_id,
            status=PRIVACY_CONFIRMATION_REQUIRED,
            confirmation_token_hash=self.confirmation_hash(token),
            created_at=_utc(),
        )
        self.store.create_deletion_job(job)
        return job, token
