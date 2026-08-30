"""Shadow evaluation runner — evidence only, no production side effects (Scale 3.36)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from evals.models import utc_now


@dataclass(frozen=True)
class ShadowEvidence:
    candidate_id: str
    tenant_id: str
    input_ref: str
    recorded_at: datetime
    outcome: str = "recorded"
    cost_observable: bool = True
    side_effects: bool = False
    mutates_user_response: bool = False
    changes_routing: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "tenant_id": self.tenant_id,
            "input_ref": self.input_ref,
            "recorded_at": self.recorded_at.isoformat(),
            "outcome": self.outcome,
            "cost_observable": self.cost_observable,
            "side_effects": self.side_effects,
            "mutates_user_response": self.mutates_user_response,
            "changes_routing": self.changes_routing,
            "details": dict(self.details),
        }


class ShadowRunner:
    """Runs a candidate scoring/eval callable and records ShadowEvidence only.

    Guarantees:
    - NO side effects (flag always False)
    - NO user response mutation
    - NO routing change
    """

    def __init__(self):
        self._evidence: list[ShadowEvidence] = []

    def evidence(self) -> tuple[ShadowEvidence, ...]:
        return tuple(self._evidence)

    def run(
        self,
        candidate: Any,
        input_ref: str,
        *,
        tenant_id: str,
        scorer: Callable[..., Mapping[str, Any]] | None = None,
        now: datetime | None = None,
        cost_observable: bool = True,
    ) -> ShadowEvidence:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        tid = str(tenant_id or "").strip()
        candidate_id = str(
            getattr(candidate, "candidate_id", None)
            or getattr(candidate, "id", None)
            or candidate
            or ""
        )
        details: dict[str, Any] = {}
        outcome = "recorded"
        if scorer is not None:
            # Scorer must be pure / read-only; we never invoke side-effect executors.
            try:
                result = scorer(candidate, input_ref, tenant_id=tid)
                details = dict(result or {})
                outcome = str(details.get("outcome") or "scored")
            except Exception as exc:
                outcome = "scorer_error"
                details = {"error_class": type(exc).__name__}

        evidence = ShadowEvidence(
            candidate_id=candidate_id,
            tenant_id=tid,
            input_ref=str(input_ref or ""),
            recorded_at=stamp,
            outcome=outcome,
            cost_observable=bool(cost_observable),
            side_effects=False,
            mutates_user_response=False,
            changes_routing=False,
            details=details,
        )
        self._evidence.append(evidence)
        return evidence
