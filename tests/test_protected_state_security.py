import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from autonomy.models import APPROVAL_PENDING, ApprovalRecord, utc_now
from hitl.models import PERMIT_ISSUED, ExecutionPermit
from security.encryption import EncryptionService
from side_effects.persistence import build_side_effect_persistence
from side_effects.sqlite_store import SqliteConnection


class ProtectedStateSecurityTests(unittest.TestCase):
    def test_sqlite_bytes_exclude_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sec.sqlite3")
            key = os.urandom(32)
            encryption = EncryptionService(key=key, key_id="test-kid")
            bundle = build_side_effect_persistence(
                durable=True,
                db_path=path,
                encryption=encryption,
                run_recovery_scan=False,
            )
            stamp = utc_now()
            secrets = {
                "GITHUB_WRITE_TOKEN": "ghp_secret_token_value_xyz",
                "PANDA_ENCRYPTION_KEY": key.hex(),
                "raw_capability_token": "cap.token.secret.abc",
                "Authorization": "Bearer super-secret-bearer",
                "Bearer": "super-secret-bearer",
                "prompt": "do not store this raw prompt fixture",
                "permit_signature": "sig-material-should-not-persist",
            }
            bundle.approval_store.create(
                ApprovalRecord(
                    approval_id="ap-sec",
                    workflow_id="wf",
                    task_id="t",
                    action_id="a",
                    decision_id="d",
                    status=APPROVAL_PENDING,
                    approved_by="pending",
                    created_at=stamp,
                    requested_by="agent-1",
                    expires_at=stamp + timedelta(hours=1),
                    version=1,
                    action_fingerprint="fp",
                    metadata={
                        "tool_id": "test.write",
                        "token": secrets["raw_capability_token"],
                        "authorization": secrets["Authorization"],
                        "prompt": secrets["prompt"],
                        "signature": secrets["permit_signature"],
                        "github_write_token": secrets["GITHUB_WRITE_TOKEN"],
                    },
                )
            )
            bundle.permit_store.create(
                ExecutionPermit(
                    permit_id="perm-sec",
                    workflow_id="wf",
                    task_id="t",
                    action_id="a",
                    approval_id="ap-sec",
                    decision_id="d",
                    action_fingerprint="fp",
                    issued_at=stamp,
                    expires_at=stamp + timedelta(minutes=5),
                    capabilities=(CAP_EXTERNAL_WRITE,),
                    tool_id="test.write",
                    operation="set_value",
                    idempotency_key="idem",
                    status=PERMIT_ISSUED,
                    version=1,
                    metadata={
                        "signature": secrets["permit_signature"],
                        "token": secrets["raw_capability_token"],
                        "bearer": secrets["Bearer"],
                    },
                )
            )
            bundle.connection.close()

            raw = Path(path).read_bytes()
            for value in secrets.values():
                encoded = value if isinstance(value, bytes) else value.encode("utf-8")
                self.assertNotIn(encoded, raw)
            self.assertNotIn(b"do not store this raw prompt", raw)

    def test_unsupported_newer_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "new.sqlite3")
            conn = SqliteConnection(path)
            conn.initialize_schema()
            conn.connect().execute(
                "UPDATE side_effect_schema_meta SET version = 99 WHERE id = 1"
            )
            conn.connect().commit()
            conn.close()
            bundle = build_side_effect_persistence(
                durable=True, db_path=path, run_recovery_scan=False
            )
            self.assertFalse(bundle.ready)
            self.assertFalse(bundle.protected_state_ready)
            self.assertEqual(
                bundle.reason_code, "side_effect_schema_version_unsupported"
            )


if __name__ == "__main__":
    unittest.main()
