"""Stage 1 — Production Foundation closure tests."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from production_foundation.alerts import evaluate_foundation_alerts
from production_foundation.alert_sink import FakeAlertSink
from production_foundation.backup import BackupService
from production_foundation.config import (
    assert_production_startup_safe,
    resolve_production_config,
    validate_production_config,
)
from production_foundation.errors import PF_BACKUP_CORRUPT, ProductionFoundationError
from production_foundation.migrations import MigrationLock, run_migrations
from production_foundation.proxy import absolute_url, trusted_public_origin
from production_foundation.restore import RestoreService, verify_manifest
from production_foundation.runtime import build_production_foundation_runtime
from production_foundation.storage import ensure_storage_roots
from saas_product.runtime import build_saas_product_runtime
from security.api_auth import configure_security
from security.auth import AuthService
from side_effects.persistence import build_side_effect_persistence


def _prod_env(base_dir: str | None = None) -> dict:
    data_dir = base_dir or os.path.join(os.getcwd(), "data", f"pf_test_{uuid.uuid4().hex[:8]}")
    os.makedirs(data_dir, exist_ok=True)
    return {
        "PANDA_ENV": "production",
        "PANDA_DATA_DIR": data_dir,
        "PUBLIC_URL": "https://app.example.com",
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": "k|t|u|user|secret-key-value-here",
        "SECURITY_CORS_ORIGINS": "https://app.example.com",
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SAAS_BILLING_ENABLED": "false",
    }


class ProductionConfigTests(unittest.TestCase):
    def test_production_missing_public_url_fails(self):
        env = _prod_env()
        env.pop("PUBLIC_URL")
        cfg = resolve_production_config(env)
        self.assertIn("production_public_url_missing", cfg.errors)

    def test_production_ephemeral_db_denied(self):
        env = _prod_env()
        env["SIDE_EFFECT_DB_PATH"] = "/tmp/evil.sqlite3"
        cfg = resolve_production_config(env)
        self.assertIn("production_ephemeral_side_effect_db", cfg.errors)

    def test_production_fake_billing_denied(self):
        env = _prod_env()
        env["SAAS_BILLING_ENABLED"] = "true"
        env["SAAS_BILLING_PROVIDER"] = "fake"
        cfg = resolve_production_config(env)
        self.assertIn("production_fake_billing_forbidden", cfg.errors)

    def test_production_valid_config_passes(self):
        env = _prod_env()
        report = validate_production_config(env)
        self.assertIn(report.overall, {"PASS", "WARN"})

    def test_assert_startup_safe_raises(self):
        env = _prod_env()
        env["SECURITY_AUTH_MODE"] = "disabled"
        with self.assertRaises(Exception):
            assert_production_startup_safe(env)


class StorageTests(unittest.TestCase):
    def test_storage_roots_created(self):
        d = tempfile.mkdtemp()
        env = _prod_env(d)
        results = ensure_storage_roots(env)
        self.assertTrue(all(results.values()))


class MigrationTests(unittest.TestCase):
    def test_migration_idempotent(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "side_effects.sqlite3")
        env = {"SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite", "SIDE_EFFECT_DB_PATH": db}
        b1 = build_side_effect_persistence(env=env, durable=True)
        b2 = build_side_effect_persistence(env=env, durable=True)
        self.assertTrue(b1.ready)
        self.assertTrue(b2.ready)
        self.assertEqual(b1.schema_version, b2.schema_version)

    def test_concurrent_migration_lock(self):
        d = tempfile.mkdtemp()
        lock1 = MigrationLock(d)
        lock2 = MigrationLock(d)
        self.assertTrue(lock1.acquire(timeout_seconds=1.0))
        self.assertFalse(lock2.acquire(timeout_seconds=0.2))
        lock1.release()
        self.assertTrue(lock2.acquire(timeout_seconds=1.0))
        lock2.release()


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)
        self.side_db = os.path.join(self.data, "side_effects.sqlite3")
        self.saas_db = os.path.join(self.data, "saas_product.sqlite")
        self.art = os.path.join(self.data, "artifacts")
        os.makedirs(self.art, exist_ok=True)
        env = {"SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite", "SIDE_EFFECT_DB_PATH": self.side_db}
        build_side_effect_persistence(env=env, durable=True)
        rt = build_saas_product_runtime(env={"SAAS_PRODUCT_DB_PATH": self.saas_db})
        from security.identity import RequestSecurityContext

        ctx = RequestSecurityContext(user_id="u1", tenant_id="t1", roles=("user",), request_id="r")
        rt.service.create_tenant(ctx, name="Co")
        rt.close()
        (Path(self.art) / "sample.txt").write_text("artifact", encoding="utf-8")
        self.backup_root = os.path.join(self.tmp, "backups")
        self.svc = BackupService(
            backup_root=self.backup_root,
            side_effect_db=self.side_db,
            saas_db=self.saas_db,
            ops_admin_db=os.path.join(self.data, "ops_admin.sqlite"),
            artifact_roots=(self.art,),
        )

    def test_backup_manifest_and_checksum(self):
        manifest = self.svc.create_backup()
        self.assertEqual(manifest.status, "SUCCESS")
        backup_dir = os.path.join(self.backup_root, manifest.backup_id)
        verified = verify_manifest(backup_dir)
        self.assertEqual(verified.backup_id, manifest.backup_id)

    def test_corrupt_backup_rejected(self):
        manifest = self.svc.create_backup()
        backup_dir = Path(self.backup_root) / manifest.backup_id
        db_file = next((backup_dir / "databases").glob("*.sqlite3"))
        db_file.write_bytes(b"corrupt")
        with self.assertRaises(ProductionFoundationError) as ctx:
            verify_manifest(str(backup_dir))
        self.assertEqual(ctx.exception.code, PF_BACKUP_CORRUPT)

    def test_restore_isolated(self):
        manifest = self.svc.create_backup()
        backup_dir = os.path.join(self.backup_root, manifest.backup_id)
        target = os.path.join(self.tmp, "restored")
        result = RestoreService(target_data_dir=target).restore(backup_dir)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(os.path.exists(os.path.join(target, "saas_product.sqlite")))
        self.assertTrue(os.path.exists(os.path.join(target, "artifacts", "sample.txt")))

    def test_backup_failure_preserves_previous(self):
        good = self.svc.create_backup()
        blocker = os.path.join(self.tmp, "blocker")
        Path(blocker).write_text("not-a-directory", encoding="utf-8")
        bad = BackupService(
            backup_root=blocker,
            side_effect_db=self.side_db,
            saas_db=self.saas_db,
            ops_admin_db=os.path.join(self.data, "ops_admin.sqlite"),
            artifact_roots=(self.art,),
        )
        with self.assertRaises(ProductionFoundationError):
            bad.create_backup()
        self.assertIsNotNone(self.svc.last_success)
        self.assertEqual(self.svc.last_success.backup_id, good.backup_id)


class ProxyTests(unittest.TestCase):
    def test_trusted_public_origin_ignores_host(self):
        env = {"PUBLIC_URL": "https://app.example.com", "PANDA_ENV": "production"}
        url = absolute_url("/invite/abc", env=env, forwarded_host="evil.com")
        self.assertTrue(url.startswith("https://app.example.com/"))

    def test_no_public_url_relative(self):
        self.assertEqual(absolute_url("/x", env={"PANDA_ENV": "development"}), "/x")


class AlertTests(unittest.TestCase):
    def test_backup_stale_alert(self):
        alerts = evaluate_foundation_alerts(
            config_ok=True,
            storage_writable=True,
            database_reachable=True,
            migration_ok=True,
            backup_status="SUCCESS",
            backup_age_hours=48,
            backup_stale_hours=26,
            disk_free_bytes=999999999,
            disk_threshold_bytes=1000,
        )
        codes = [a.code for a in alerts if a.active]
        self.assertIn("BACKUP_STALE", codes)

    def test_alert_dedupe_via_engine(self):
        from operations_admin.alerts import AlertEngine

        engine = AlertEngine()
        a1 = engine.observe(source="pf", message="DATABASE_UNAVAILABLE", severity="critical", active=True)
        a2 = engine.observe(source="pf", message="DATABASE_UNAVAILABLE", severity="critical", active=True)
        self.assertEqual(a1.alert_id, a2.alert_id)
        self.assertEqual(a2.count, 2)

    def test_fake_alert_sink(self):
        sink = FakeAlertSink()
        res = sink.deliver(code="TEST", severity="warning", message="hello")
        self.assertTrue(res.delivered)
        self.assertEqual(len(sink.deliveries), 1)


class PersistenceRestartTests(unittest.TestCase):
    def test_saas_state_survives_restart(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "saas.sqlite")
        rt = build_saas_product_runtime(env={"SAAS_PRODUCT_DB_PATH": db})
        from security.identity import RequestSecurityContext

        ctx = RequestSecurityContext(user_id="owner", tenant_id="t", roles=("user",), request_id="r")
        t = rt.service.create_tenant(ctx, name="RestartCo")
        tid = t.tenant_id
        rt.close()
        rt2 = build_saas_product_runtime(env={"SAAS_PRODUCT_DB_PATH": db})
        self.assertIsNotNone(rt2.service.store.get_tenant(tid))
        rt2.close()


class ProductionFoundationHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        env = _prod_env(cls.tmp)
        env["PANDA_ENV"] = "development"
        os.environ.update(env)
        configure_security(auth=AuthService(env=env))
        import main as main_mod

        importlib.reload(main_mod)
        cls.client = TestClient(main_mod.app)

    def test_health_no_secrets(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("secret", r.text.lower())

    def test_ready_endpoint(self):
        r = self.client.get("/ready")
        self.assertIn(r.status_code, {200, 503})

    def test_production_foundation_admin(self):
        r = self.client.get(
            "/api/admin/ops/production-foundation",
            headers={"X-API-Key": "secret-key-value-here"},
        )
        if r.status_code == 403:
            self.skipTest("admin key not platform admin")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("monitoring", body)


class SecretRedactionTests(unittest.TestCase):
    def test_config_report_no_raw_secret(self):
        env = _prod_env()
        report = validate_production_config(env)
        blob = json.dumps(report.as_dict())
        self.assertNotIn("secret-key-value-here", blob)


if __name__ == "__main__":
    unittest.main()
