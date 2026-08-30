"""Stage-5 persistence / cross-process rehydration tests (P1 production defect)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from controlled_launch.handoff import Stage3HandoffGate
from production_activation.access import PERM_ACTIVATION_AUTHORIZE, PERM_ACTIVATION_READ, PERM_ACTIVATION_WRITE
from production_activation.handoff import Stage5HandoffGate
from production_activation.paths import open_production_activation_store, resolve_production_activation_db_path
from production_activation.runtime import (
    configure_production_activation_runtime,
    get_production_activation_runtime,
    reset_production_activation_runtime,
)
from production_activation.service import ProductionActivationService
from production_activation.sqlite_store import SqliteProductionActivationStore
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass as PVClass
from production_validation.release_gate import MANDATORY_LIVE_GATES


def _admin():
    return SimpleNamespace(
        actor_ref=lambda: "ops",
        permissions=(PERM_ACTIVATION_READ, PERM_ACTIVATION_WRITE, PERM_ACTIVATION_AUTHORIZE, "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def _seed_stage3(tmp: str, release_identity: str = "rel-persist"):
    store = EvidenceStore(root=tmp)
    config = ValidationConfig(production_url="https://prod.example", release_identity=release_identity, environment="production")
    for gate_name in MANDATORY_LIVE_GATES:
        g = ReleaseEvidence.begin(gate=gate_name, environment="production", mode=ExecutionMode.LIVE_SAFE, release_identity=release_identity)
        g.complete(status=GateStatus.PASS, classification=PVClass.LIVE_VERIFIED.value)
        store.save(g)
    return store, config


def _svc(db_path: str, *, ev_root: str, release: str) -> ProductionActivationService:
    ev_store, config = _seed_stage3(ev_root, release_identity=release)
    return ProductionActivationService(
        store=SqliteProductionActivationStore(path=db_path),
        handoff_gate=Stage5HandoffGate(
            stage3_gate=Stage3HandoffGate(config=config, evidence_store=ev_store),
            require_stage4_artifact=True,
        ),
    )


class Stage5PathResolutionTests(unittest.TestCase):
    def test_panda_data_dir_resolves_persistent_location(self):
        path = resolve_production_activation_db_path({"PANDA_DATA_DIR": "/data"})
        self.assertEqual(path.replace("\\", "/"), "/data/production_activation.sqlite")

    def test_explicit_override(self):
        path = resolve_production_activation_db_path({"PRODUCTION_ACTIVATION_DB_PATH": "/data/custom_pa.sqlite"})
        self.assertEqual(path, "/data/custom_pa.sqlite")

    def test_production_env_defaults_to_data(self):
        path = resolve_production_activation_db_path({"PANDA_ENV": "production"})
        self.assertEqual(path.replace("\\", "/"), "/data/production_activation.sqlite")


class Stage5CrossProcessPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmpdir.name) / "production_activation.sqlite")
        self.ev = str(Path(self._tmpdir.name) / "ev")
        self.release = "sha-persist-001"
        self.admin = _admin()
        reset_production_activation_runtime()

    def tearDown(self):
        reset_production_activation_runtime()
        self._tmpdir.cleanup()

    def test_case1_policy_survives_reconstruction(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        policy = a.create_go_live_policy(self.admin, release_identity=self.release)
        pid = policy["policy_id"]
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        status = b.stage5_status(self.admin)
        self.assertIsNotNone(status["policy"])
        self.assertEqual(status["policy"]["policy_id"], pid)
        self.assertEqual(status["policy"]["release_identity"], self.release)
        self.assertFalse(status["go_live_active"])
        b.store.close()

    def test_case2_evidence_survives_reconstruction(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        seeded = a.seed_stage5_evidence(self.admin, candidate_id="stage5-production", release_identity=self.release)
        self.assertEqual(len(seeded["evidence"]), 15)
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        evidence = b.store.list_evidence("stage5-production")
        gates = {(e.safe_metrics or {}).get("gate") for e in evidence}
        from production_activation.stage5_gate import MANDATORY_STAGE5_GATES

        for g in MANDATORY_STAGE5_GATES:
            self.assertIn(g, gates)
        self.assertGreaterEqual(len(evidence), 15)
        b.store.close()

    def test_case3_evaluate_after_reconstruction_ready(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        a.seed_stage5_evidence(self.admin, candidate_id="stage5-production", release_identity=self.release)
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        gate = b.evaluate_stage5_gate(self.admin, candidate_id="stage5-production")
        self.assertEqual(gate["verdict"], "GO_LIVE_READY")
        self.assertTrue(gate["go_live_eligible"])
        self.assertFalse(gate["go_live_active"])
        self.assertEqual(gate["blocked"], [])
        self.assertEqual(gate["release_identity"], self.release)
        self.assertTrue(gate["policy_version"])
        for key, status in gate["gates"].items():
            if key.startswith("5.") and key != "5.16_post_activation_live":
                self.assertEqual(status, "PASS", msg=key)
        b.store.close()

    def test_case4_inactive_state_survives_no_activation(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        self.assertFalse(b.stage5_status(self.admin)["go_live_active"])
        self.assertNotEqual(b.activator.state, "PRODUCTION_ACTIVE")
        b.store.close()

    def test_case5_deactivation_survives(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        a.deactivate(self.admin, candidate_id="stage5-production", operator_ref="ops", reason="drill")
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        state = b.store.get_activation_state()
        self.assertEqual(state["state"], "ROLLED_BACK")
        self.assertFalse(state.get("go_live_active"))
        policy = b.store.latest_go_live_policy()
        self.assertFalse(policy.go_live_active)
        self.assertEqual(policy.deactivation_state, "drill")
        b.store.close()

    def test_case6_missing_db_fails_closed(self):
        empty = str(Path(self._tmpdir.name) / "empty" / "missing.sqlite")
        Path(empty).parent.mkdir(parents=True, exist_ok=True)
        b = _svc(empty, ev_root=self.ev, release=self.release)
        gate = b.evaluate_stage5_gate(self.admin, candidate_id="stage5-production")
        self.assertEqual(gate["verdict"], "GO_LIVE_BLOCKED")
        self.assertFalse(gate["go_live_active"])
        b.store.close()

    def test_case7_release_mismatch_fails_closed(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        a.seed_stage5_evidence(self.admin, candidate_id="stage5-production", release_identity="other-sha")
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        gate = b.evaluate_stage5_gate(self.admin, candidate_id="stage5-production")
        self.assertEqual(gate["verdict"], "GO_LIVE_BLOCKED")
        self.assertFalse(gate["go_live_eligible"])
        b.store.close()

    def test_case8_other_candidate_cannot_satisfy(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        a.seed_stage5_evidence(self.admin, candidate_id="other-candidate", release_identity=self.release)
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        gate = b.evaluate_stage5_gate(self.admin, candidate_id="stage5-production")
        self.assertEqual(gate["verdict"], "GO_LIVE_BLOCKED")
        b.store.close()

    def test_case9_idempotent_duplicate_writes(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        a.seed_stage5_evidence(self.admin, candidate_id="stage5-production", release_identity=self.release)
        a.seed_stage5_evidence(self.admin, candidate_id="stage5-production", release_identity=self.release)
        a.store.close()

        b = _svc(self.db, ev_root=self.ev, release=self.release)
        gate = b.evaluate_stage5_gate(self.admin, candidate_id="stage5-production")
        self.assertEqual(gate["verdict"], "GO_LIVE_READY")
        self.assertFalse(gate["go_live_active"])
        b.store.close()

    def test_case10_repeated_runtime_construction(self):
        env = {"PRODUCTION_ACTIVATION_DB_PATH": self.db, "PANDA_DATA_DIR": self._tmpdir.name}
        reset_production_activation_runtime()
        # Build handoff-capable service via configure to avoid real Stage3 env issues
        svc = _svc(self.db, ev_root=self.ev, release=self.release)
        configure_production_activation_runtime(svc)
        r1 = get_production_activation_runtime(env=env)
        r1.create_go_live_policy(self.admin, release_identity=self.release)
        reset_production_activation_runtime()
        # Fresh open of same DB path via factory
        store = open_production_activation_store(env)
        r2 = ProductionActivationService(store=store, handoff_gate=svc.handoff_gate)
        self.assertIsNotNone(r2.store.latest_go_live_policy())
        self.assertFalse(r2.stage5_status(self.admin)["go_live_active"])
        r2.store.close()

    def test_case12_restart_simulation_close_recreate_evaluate(self):
        a = _svc(self.db, ev_root=self.ev, release=self.release)
        a.create_go_live_policy(self.admin, release_identity=self.release)
        a.seed_stage5_evidence(self.admin, candidate_id="stage5-production", release_identity=self.release)
        a.store.close()

        # Simulate process exit: new connection, no shared singleton
        reset_production_activation_runtime()
        b = _svc(self.db, ev_root=self.ev, release=self.release)
        status = b.stage5_status(self.admin)
        self.assertIsNotNone(status["policy"])
        gate = b.evaluate_stage5_gate(self.admin, candidate_id="stage5-production")
        self.assertEqual(gate["verdict"], "GO_LIVE_READY")
        self.assertFalse(gate["go_live_active"])
        b.store.close()


class Stage5RuntimeDefaultNotMemoryTests(unittest.TestCase):
    def test_open_default_store_is_file_backed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PANDA_DATA_DIR": tmp}
            store = open_production_activation_store(env)
            try:
                self.assertNotEqual(store.path, ":memory:")
                self.assertTrue(store.path.endswith("production_activation.sqlite"))
                self.assertTrue(os.path.isdir(tmp))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
