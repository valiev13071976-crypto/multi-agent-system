"""Launch evidence persistence."""

from __future__ import annotations

from controlled_launch.models import LaunchEvidence


class LaunchEvidenceStore:
    def __init__(self):
        self._items: list[LaunchEvidence] = []

    def save(self, evidence: LaunchEvidence) -> LaunchEvidence:
        self._items.append(evidence)
        return evidence

    def list_for_candidate(self, candidate_id: str) -> list[LaunchEvidence]:
        return [e for e in self._items if e.candidate_id == candidate_id]

    def list_gate(self, candidate_id: str, gate: str) -> list[LaunchEvidence]:
        return [e for e in self._items if e.candidate_id == candidate_id and e.gate == gate]

    def all(self) -> list[LaunchEvidence]:
        return list(self._items)
