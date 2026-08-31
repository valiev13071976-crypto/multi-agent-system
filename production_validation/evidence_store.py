"""Append-only release evidence store."""

from __future__ import annotations

import json
import os
from pathlib import Path

from production_validation.models import ReleaseEvidence


def resolve_release_evidence_root(env: dict | None = None) -> str:
    """Resolve durable Stage-3/ongoing release evidence root.

    Precedence:
    1. PANDA_RELEASE_EVIDENCE_ROOT (explicit)
    2. $PANDA_DATA_DIR/release_evidence (or DATA_DIR)
    3. ./data/release_evidence (local fallback when no data dir)
    """
    source = env if env is not None else os.environ
    explicit = str(source.get("PANDA_RELEASE_EVIDENCE_ROOT") or "").strip()
    if explicit:
        return explicit
    data_dir = str(source.get("PANDA_DATA_DIR") or source.get("DATA_DIR") or "").strip()
    if data_dir:
        return os.path.join(data_dir, "release_evidence")
    return os.path.join("data", "release_evidence")


class EvidenceStore:
    def __init__(self, *, root: str | None = None, env: dict | None = None):
        self.root = Path(root if root is not None else resolve_release_evidence_root(env))
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.jsonl"

    def save(self, evidence: ReleaseEvidence) -> ReleaseEvidence:
        if evidence.completed_at and self._find(evidence.evidence_id):
            existing = self._find(evidence.evidence_id)
            if existing and existing.get("status") in {"PASS", "FAIL", "BLOCKED"} and existing.get("status") == evidence.status:
                return evidence
        path = self.root / f"{evidence.evidence_id}.json"
        if path.exists() and evidence.status == "running":
            raise RuntimeError(f"evidence_exists:{evidence.evidence_id}")
        payload = evidence.as_dict()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with self._index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"evidence_id": evidence.evidence_id, "gate": evidence.gate, "status": evidence.status, "completed_at": evidence.completed_at}) + "\n")
        return evidence

    def supersede(self, old_id: str, new_id: str) -> None:
        old = self._find(old_id)
        if old is None:
            return
        old["superseded_by"] = new_id
        (self.root / f"{old_id}.json").write_text(json.dumps(old, indent=2, sort_keys=True), encoding="utf-8")

    def list_gate(self, gate: str) -> list[dict]:
        out = []
        for path in sorted(self.root.glob("ev-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("gate") == gate and not data.get("superseded_by"):
                out.append(data)
        return out

    def latest_for_gate(self, gate: str) -> dict | None:
        items = self.list_gate(gate)
        if not items:
            return None
        return sorted(items, key=lambda x: x.get("completed_at") or x.get("started_at"))[-1]

    def all_completed(self) -> list[dict]:
        out = []
        for path in sorted(self.root.glob("ev-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") in {"PASS", "FAIL", "BLOCKED"} and not data.get("superseded_by"):
                out.append(data)
        return out

    def _find(self, evidence_id: str) -> dict | None:
        path = self.root / f"{evidence_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
