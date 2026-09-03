"""SQLite persistence for human accounts, sessions, access grants, compliance."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from accounts.models import (
    AccountsAuditEvent,
    ComplimentaryAccessRecord,
    DeletionRequestRecord,
    HumanUserRecord,
    PaymentMethodControl,
    PolicyDocument,
    SessionRecord,
    TrialRecord,
    UserDecisionRecord,
)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class AccountsStore:
    SCHEMA_VERSION = 1

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts_schema_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS human_users (
                user_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                username TEXT NOT NULL,
                normalized_username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT,
                password_changed_at TEXT,
                email TEXT,
                display_name TEXT,
                is_bootstrap_owner INTEGER NOT NULL DEFAULT 0,
                protected INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_human_users_tenant ON human_users(tenant_id);
            CREATE TABLE IF NOT EXISTS human_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                revoked_at TEXT,
                csrf_token TEXT NOT NULL,
                auth_method TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_human_sessions_user ON human_sessions(user_id);
            CREATE TABLE IF NOT EXISTS access_trials (
                trial_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                trial_started_at TEXT NOT NULL,
                trial_ends_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(tenant_id)
            );
            CREATE TABLE IF NOT EXISTS complimentary_access (
                grant_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                access_started_at TEXT NOT NULL,
                access_until TEXT,
                unlimited INTEGER NOT NULL,
                reason TEXT NOT NULL,
                granted_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_comp_tenant ON complimentary_access(tenant_id);
            CREATE TABLE IF NOT EXISTS policy_documents (
                document_id TEXT PRIMARY KEY,
                document_type TEXT NOT NULL,
                version TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                status TEXT NOT NULL,
                content_reference TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                draft_requires_legal_review INTEGER NOT NULL,
                UNIQUE(document_type, version)
            );
            CREATE TABLE IF NOT EXISTS user_decisions (
                decision_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                document_type TEXT NOT NULL,
                document_version TEXT NOT NULL,
                decision_type TEXT NOT NULL,
                decision TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                withdrawn_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_user_decisions_user ON user_decisions(user_id, decision_type);
            CREATE TABLE IF NOT EXISTS payment_method_controls (
                control_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_reference TEXT NOT NULL,
                usage_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                revocation_source TEXT,
                UNIQUE(tenant_id, provider, provider_reference)
            );
            CREATE TABLE IF NOT EXISTS deletion_requests (
                request_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                retention_hold INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS accounts_audit (
                event_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                correlation_id TEXT,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                bucket_key TEXT PRIMARY KEY,
                fail_count INTEGER NOT NULL,
                window_started_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS product_usage (
                idempotency_key TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                meter TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                period_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_product_usage ON product_usage(tenant_id, meter, period_key);
            """
        )
        cur = self._conn.execute("SELECT value FROM accounts_schema_meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO accounts_schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
        self._conn.commit()

    # --- users ---

    def create_user(self, user: HumanUserRecord) -> HumanUserRecord:
        self._conn.execute(
            """
            INSERT INTO human_users(
                user_id, tenant_id, username, normalized_username, password_hash, role, status,
                created_at, updated_at, last_login_at, password_changed_at, email, display_name,
                is_bootstrap_owner, protected
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user.user_id,
                user.tenant_id,
                user.username,
                user.normalized_username,
                user.password_hash,
                user.role,
                user.status,
                user.created_at,
                user.updated_at,
                user.last_login_at,
                user.password_changed_at,
                user.email,
                user.display_name,
                1 if user.is_bootstrap_owner else 0,
                1 if user.protected else 0,
            ),
        )
        self._conn.commit()
        return user

    def get_user(self, user_id: str) -> HumanUserRecord | None:
        row = self._conn.execute("SELECT * FROM human_users WHERE user_id=?", (user_id,)).fetchone()
        return self._user_from_row(row) if row else None

    def get_user_by_username(self, normalized_username: str) -> HumanUserRecord | None:
        row = self._conn.execute(
            "SELECT * FROM human_users WHERE normalized_username=?", (normalized_username,)
        ).fetchone()
        return self._user_from_row(row) if row else None

    def list_users(self, *, tenant_id: str | None = None, limit: int = 100) -> list[HumanUserRecord]:
        if tenant_id:
            rows = self._conn.execute(
                "SELECT * FROM human_users WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM human_users ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._user_from_row(r) for r in rows]

    def update_user(self, user: HumanUserRecord) -> HumanUserRecord:
        self._conn.execute(
            """
            UPDATE human_users SET tenant_id=?, username=?, normalized_username=?, password_hash=?,
            role=?, status=?, updated_at=?, last_login_at=?, password_changed_at=?, email=?,
            display_name=?, is_bootstrap_owner=?, protected=? WHERE user_id=?
            """,
            (
                user.tenant_id,
                user.username,
                user.normalized_username,
                user.password_hash,
                user.role,
                user.status,
                user.updated_at,
                user.last_login_at,
                user.password_changed_at,
                user.email,
                user.display_name,
                1 if user.is_bootstrap_owner else 0,
                1 if user.protected else 0,
                user.user_id,
            ),
        )
        self._conn.commit()
        return user

    def count_protected_owners(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM human_users WHERE is_bootstrap_owner=1 AND protected=1 AND status='ACTIVE'"
        ).fetchone()
        return int(row["c"])

    def _user_from_row(self, row: sqlite3.Row) -> HumanUserRecord:
        return HumanUserRecord(
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            username=row["username"],
            normalized_username=row["normalized_username"],
            password_hash=row["password_hash"],
            role=row["role"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_login_at=row["last_login_at"] or "",
            password_changed_at=row["password_changed_at"] or "",
            email=row["email"] or "",
            display_name=row["display_name"] or "",
            is_bootstrap_owner=bool(row["is_bootstrap_owner"]),
            protected=bool(row["protected"]),
        )

    # --- sessions ---

    def create_session(self, session: SessionRecord) -> SessionRecord:
        self._conn.execute(
            """
            INSERT INTO human_sessions(session_id, user_id, tenant_id, created_at, expires_at,
            last_seen_at, revoked_at, csrf_token, auth_method)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                session.session_id,
                session.user_id,
                session.tenant_id,
                session.created_at,
                session.expires_at,
                session.last_seen_at,
                session.revoked_at,
                session.csrf_token,
                session.auth_method,
            ),
        )
        self._conn.commit()
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._conn.execute("SELECT * FROM human_sessions WHERE session_id=?", (session_id,)).fetchone()
        return self._session_from_row(row) if row else None

    def update_session(self, session: SessionRecord) -> SessionRecord:
        self._conn.execute(
            """
            UPDATE human_sessions SET expires_at=?, last_seen_at=?, revoked_at=?, csrf_token=?
            WHERE session_id=?
            """,
            (session.expires_at, session.last_seen_at, session.revoked_at, session.csrf_token, session.session_id),
        )
        self._conn.commit()
        return session

    def revoke_sessions_for_user(self, user_id: str, *, now: str) -> int:
        cur = self._conn.execute(
            "UPDATE human_sessions SET revoked_at=? WHERE user_id=? AND (revoked_at IS NULL OR revoked_at='')",
            (now, user_id),
        )
        self._conn.commit()
        return cur.rowcount

    def _session_from_row(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            last_seen_at=row["last_seen_at"],
            revoked_at=row["revoked_at"] or "",
            csrf_token=row["csrf_token"],
            auth_method=row["auth_method"],
        )

    # --- trial / complimentary ---

    def upsert_trial(self, trial: TrialRecord) -> TrialRecord:
        self._conn.execute(
            """
            INSERT INTO access_trials(trial_id, tenant_id, user_id, plan_id, trial_started_at,
            trial_ends_at, created_at, status) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id) DO UPDATE SET
              trial_id=excluded.trial_id, user_id=excluded.user_id, plan_id=excluded.plan_id,
              trial_started_at=excluded.trial_started_at, trial_ends_at=excluded.trial_ends_at,
              status=excluded.status
            """,
            (
                trial.trial_id,
                trial.tenant_id,
                trial.user_id,
                trial.plan_id,
                trial.trial_started_at,
                trial.trial_ends_at,
                trial.created_at,
                trial.status,
            ),
        )
        self._conn.commit()
        return trial

    def get_trial(self, tenant_id: str) -> TrialRecord | None:
        row = self._conn.execute("SELECT * FROM access_trials WHERE tenant_id=?", (tenant_id,)).fetchone()
        if not row:
            return None
        return TrialRecord(
            trial_id=row["trial_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            plan_id=row["plan_id"],
            trial_started_at=row["trial_started_at"],
            trial_ends_at=row["trial_ends_at"],
            created_at=row["created_at"],
            status=row["status"],
        )

    def create_complimentary(self, grant: ComplimentaryAccessRecord) -> ComplimentaryAccessRecord:
        self._conn.execute(
            """
            INSERT INTO complimentary_access(grant_id, tenant_id, user_id, plan_id, access_started_at,
            access_until, unlimited, reason, granted_by, created_at, revoked_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                grant.grant_id,
                grant.tenant_id,
                grant.user_id,
                grant.plan_id,
                grant.access_started_at,
                grant.access_until,
                1 if grant.unlimited else 0,
                grant.reason,
                grant.granted_by,
                grant.created_at,
                grant.revoked_at,
            ),
        )
        self._conn.commit()
        return grant

    def update_complimentary(self, grant: ComplimentaryAccessRecord) -> ComplimentaryAccessRecord:
        self._conn.execute(
            "UPDATE complimentary_access SET revoked_at=?, access_until=?, unlimited=? WHERE grant_id=?",
            (grant.revoked_at, grant.access_until, 1 if grant.unlimited else 0, grant.grant_id),
        )
        self._conn.commit()
        return grant

    def _comp_from_row(self, row: sqlite3.Row) -> ComplimentaryAccessRecord:
        return ComplimentaryAccessRecord(
            grant_id=row["grant_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            plan_id=row["plan_id"],
            access_started_at=row["access_started_at"],
            access_until=row["access_until"] or "",
            unlimited=bool(row["unlimited"]),
            reason=row["reason"],
            granted_by=row["granted_by"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"] or "",
        )

    def get_complimentary(self, grant_id: str) -> ComplimentaryAccessRecord | None:
        row = self._conn.execute("SELECT * FROM complimentary_access WHERE grant_id=?", (grant_id,)).fetchone()
        return self._comp_from_row(row) if row else None

    def list_complimentary(self, tenant_id: str) -> list[ComplimentaryAccessRecord]:
        rows = self._conn.execute(
            "SELECT * FROM complimentary_access WHERE tenant_id=? ORDER BY created_at DESC", (tenant_id,)
        ).fetchall()
        return [self._comp_from_row(r) for r in rows]

    # --- policies / decisions ---

    def upsert_policy(self, doc: PolicyDocument) -> PolicyDocument:
        self._conn.execute(
            """
            INSERT INTO policy_documents(document_id, document_type, version, effective_from, status,
            content_reference, title, created_at, draft_requires_legal_review)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(document_type, version) DO UPDATE SET
              status=excluded.status, content_reference=excluded.content_reference, title=excluded.title
            """,
            (
                doc.document_id,
                doc.document_type,
                doc.version,
                doc.effective_from,
                doc.status,
                doc.content_reference,
                doc.title,
                doc.created_at,
                1 if doc.draft_requires_legal_review else 0,
            ),
        )
        self._conn.commit()
        return doc

    def get_policy(self, document_type: str, version: str) -> PolicyDocument | None:
        row = self._conn.execute(
            "SELECT * FROM policy_documents WHERE document_type=? AND version=?",
            (document_type, version),
        ).fetchone()
        return self._policy_from_row(row) if row else None

    def get_current_policy(self, document_type: str) -> PolicyDocument | None:
        row = self._conn.execute(
            """
            SELECT * FROM policy_documents WHERE document_type=? AND status='CURRENT'
            ORDER BY effective_from DESC LIMIT 1
            """,
            (document_type,),
        ).fetchone()
        return self._policy_from_row(row) if row else None

    def list_policies(self, document_type: str | None = None) -> list[PolicyDocument]:
        if document_type:
            rows = self._conn.execute(
                "SELECT * FROM policy_documents WHERE document_type=? ORDER BY effective_from",
                (document_type,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM policy_documents ORDER BY document_type, effective_from").fetchall()
        return [self._policy_from_row(r) for r in rows]

    def _policy_from_row(self, row: sqlite3.Row) -> PolicyDocument:
        return PolicyDocument(
            document_id=row["document_id"],
            document_type=row["document_type"],
            version=row["version"],
            effective_from=row["effective_from"],
            status=row["status"],
            content_reference=row["content_reference"],
            title=row["title"],
            created_at=row["created_at"],
            draft_requires_legal_review=bool(row["draft_requires_legal_review"]),
        )

    def record_decision(self, rec: UserDecisionRecord) -> UserDecisionRecord:
        self._conn.execute(
            """
            INSERT INTO user_decisions(decision_id, user_id, tenant_id, document_type, document_version,
            decision_type, decision, timestamp, source, withdrawn_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rec.decision_id,
                rec.user_id,
                rec.tenant_id,
                rec.document_type,
                rec.document_version,
                rec.decision_type,
                rec.decision,
                rec.timestamp,
                rec.source,
                rec.withdrawn_at,
            ),
        )
        self._conn.commit()
        return rec

    def update_decision(self, rec: UserDecisionRecord) -> UserDecisionRecord:
        self._conn.execute(
            "UPDATE user_decisions SET decision=?, withdrawn_at=? WHERE decision_id=?",
            (rec.decision, rec.withdrawn_at, rec.decision_id),
        )
        self._conn.commit()
        return rec

    def list_decisions(self, *, user_id: str, decision_type: str | None = None) -> list[UserDecisionRecord]:
        if decision_type:
            rows = self._conn.execute(
                "SELECT * FROM user_decisions WHERE user_id=? AND decision_type=? ORDER BY timestamp",
                (user_id, decision_type),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM user_decisions WHERE user_id=? ORDER BY timestamp", (user_id,)
            ).fetchall()
        return [self._decision_from_row(r) for r in rows]

    def _decision_from_row(self, row: sqlite3.Row) -> UserDecisionRecord:
        return UserDecisionRecord(
            decision_id=row["decision_id"],
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            document_type=row["document_type"],
            document_version=row["document_version"],
            decision_type=row["decision_type"],
            decision=row["decision"],
            timestamp=row["timestamp"],
            source=row["source"],
            withdrawn_at=row["withdrawn_at"] or "",
        )

    # --- payment method controls ---

    def upsert_payment_method(self, control: PaymentMethodControl) -> PaymentMethodControl:
        self._conn.execute(
            """
            INSERT INTO payment_method_controls(control_id, tenant_id, user_id, provider, provider_reference,
            usage_status, created_at, revoked_at, revocation_source)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id, provider, provider_reference) DO UPDATE SET
              usage_status=excluded.usage_status, revoked_at=excluded.revoked_at,
              revocation_source=excluded.revocation_source
            """,
            (
                control.control_id,
                control.tenant_id,
                control.user_id,
                control.provider,
                control.provider_reference,
                control.usage_status,
                control.created_at,
                control.revoked_at,
                control.revocation_source,
            ),
        )
        self._conn.commit()
        return control

    def get_payment_method(self, *, tenant_id: str, provider: str, provider_reference: str) -> PaymentMethodControl | None:
        row = self._conn.execute(
            """
            SELECT * FROM payment_method_controls
            WHERE tenant_id=? AND provider=? AND provider_reference=?
            """,
            (tenant_id, provider, provider_reference),
        ).fetchone()
        if not row:
            return None
        return PaymentMethodControl(
            control_id=row["control_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            provider=row["provider"],
            provider_reference=row["provider_reference"],
            usage_status=row["usage_status"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"] or "",
            revocation_source=row["revocation_source"] or "",
        )

    # --- deletion ---

    def create_deletion_request(self, req: DeletionRequestRecord) -> DeletionRequestRecord:
        self._conn.execute(
            """
            INSERT INTO deletion_requests(request_id, tenant_id, user_id, status, created_at, updated_at,
            completed_at, retention_hold) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                req.request_id,
                req.tenant_id,
                req.user_id,
                req.status,
                req.created_at,
                req.updated_at,
                req.completed_at,
                1 if req.retention_hold else 0,
            ),
        )
        self._conn.commit()
        return req

    def get_deletion_request(self, request_id: str) -> DeletionRequestRecord | None:
        row = self._conn.execute("SELECT * FROM deletion_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            return None
        return DeletionRequestRecord(
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"] or "",
            retention_hold=bool(row["retention_hold"]),
        )

    def update_deletion_request(self, req: DeletionRequestRecord) -> DeletionRequestRecord:
        self._conn.execute(
            """
            UPDATE deletion_requests SET status=?, updated_at=?, completed_at=?, retention_hold=?
            WHERE request_id=?
            """,
            (req.status, req.updated_at, req.completed_at, 1 if req.retention_hold else 0, req.request_id),
        )
        self._conn.commit()
        return req

    # --- usage ---

    def record_usage(self, *, idempotency_key: str, tenant_id: str, user_id: str, meter: str, quantity: int, period_key: str) -> bool:
        existing = self._conn.execute(
            "SELECT 1 FROM product_usage WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing:
            return False
        self._conn.execute(
            """
            INSERT INTO product_usage(idempotency_key, tenant_id, user_id, meter, quantity, period_key, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (idempotency_key, tenant_id, user_id, meter, quantity, period_key, _utc_now_iso()),
        )
        self._conn.commit()
        return True

    def usage_sum(self, *, tenant_id: str, meter: str, period_key: str, user_id: str | None = None) -> int:
        if user_id:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(quantity),0) AS s FROM product_usage
                WHERE tenant_id=? AND meter=? AND period_key=? AND user_id=?
                """,
                (tenant_id, meter, period_key, user_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(quantity),0) AS s FROM product_usage
                WHERE tenant_id=? AND meter=? AND period_key=?
                """,
                (tenant_id, meter, period_key),
            ).fetchone()
        return int(row["s"])

    # --- login rate limit ---

    def get_login_fails(self, bucket_key: str) -> tuple[int, str]:
        row = self._conn.execute("SELECT fail_count, window_started_at FROM login_attempts WHERE bucket_key=?", (bucket_key,)).fetchone()
        if not row:
            return 0, ""
        return int(row["fail_count"]), row["window_started_at"]

    def set_login_fails(self, bucket_key: str, fail_count: int, window_started_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO login_attempts(bucket_key, fail_count, window_started_at) VALUES (?,?,?)
            ON CONFLICT(bucket_key) DO UPDATE SET fail_count=excluded.fail_count, window_started_at=excluded.window_started_at
            """,
            (bucket_key, fail_count, window_started_at),
        )
        self._conn.commit()

    def clear_login_fails(self, bucket_key: str) -> None:
        self._conn.execute("DELETE FROM login_attempts WHERE bucket_key=?", (bucket_key,))
        self._conn.commit()

    # --- audit ---

    def append_audit(self, event: AccountsAuditEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO accounts_audit(event_id, timestamp, actor_id, target_id, tenant_id, action, result,
            correlation_id, metadata_json) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                event.event_id,
                event.timestamp,
                event.actor_id,
                event.target_id,
                event.tenant_id,
                event.action,
                event.result,
                event.correlation_id,
                json.dumps(event.metadata or {}, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def list_audit(self, *, tenant_id: str | None = None, limit: int = 100) -> list[AccountsAuditEvent]:
        if tenant_id:
            rows = self._conn.execute(
                "SELECT * FROM accounts_audit WHERE tenant_id=? ORDER BY timestamp DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM accounts_audit ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                AccountsAuditEvent(
                    event_id=row["event_id"],
                    timestamp=row["timestamp"],
                    actor_id=row["actor_id"],
                    target_id=row["target_id"],
                    tenant_id=row["tenant_id"],
                    action=row["action"],
                    result=row["result"],
                    correlation_id=row["correlation_id"] or "",
                    metadata=json.loads(row["metadata_json"] or "{}"),
                )
            )
        return out
