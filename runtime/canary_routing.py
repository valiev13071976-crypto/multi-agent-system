"""Live canary traffic + deterministic rollback (Scale 3.37 / 3.38).

CANARY is authoritative for the requests assigned to it (unlike shadow,
``runtime.shadow_traffic``, which is always non-authoritative). This module
implements bounded, trusted, server-side-configuration-driven canary
assignment plus a rollback contract that safely and idempotently returns
authoritative traffic to the stable path.

Trust boundary: assignment and rollback state are governed entirely by
``CanaryConfig``/``CanaryRolloutController``, both of which are constructed
from trusted deployment configuration (env vars) or explicit operator calls
-- never from request/tool arguments or message text. ``assign()`` accepts
only ``tenant_id``/``session_id`` identity keys for deterministic/sticky
bucketing, never a caller-supplied "give me candidate" flag.

Fail-safe default (Scale 3.30 of the spec, "CANARY FAIL-SAFE DEFAULT"): any
ambiguous/invalid configuration (disabled, missing candidate_id, percentage
0 or out of bounds, or an active rollback) resolves to STABLE.

Deterministic bucketing reuses the existing, already-proven stable-hash
cohort utility from ``controlled_launch.traffic_policy`` (Stage-4 controlled
launch control plane) rather than re-inventing a second bucketing scheme.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from controlled_launch.traffic_policy import stable_bucket

MODE_STABLE = "stable"
MODE_CANDIDATE = "candidate"

ROLLBACK_MANUAL = "manual"
ROLLBACK_AUTOMATIC = "automatic"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CanaryConfig:
    """Bounded, typed, safe-default canary rollout policy.

    Default: OFF / 0% -- automatic rollout must never activate merely
    because code was deployed.
    """

    enabled: bool = False
    candidate_id: str = ""
    percent_basis_points: int = 0  # 0..10000 (0.01% granularity)
    policy_version: str = "v1"

    def is_usable(self) -> bool:
        """Ambiguous/invalid configuration must prefer stable traffic."""

        return (
            self.enabled
            and bool(self.candidate_id.strip())
            and 0 < self.percent_basis_points <= 10000
        )

    @classmethod
    def from_env(cls, env: Mapping | None = None) -> "CanaryConfig":
        source = env if env is not None else os.environ

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        def _bp(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = int(str(raw).strip())
            except ValueError:
                return default
            if value < 0 or value > 10000:
                return default
            return value

        return cls(
            enabled=_bool("CANARY_ENABLED", False),
            candidate_id=str(source.get("CANARY_CANDIDATE_ID") or "").strip(),
            percent_basis_points=_bp("CANARY_PERCENT_BASIS_POINTS", 0),
            policy_version=str(source.get("CANARY_POLICY_VERSION") or "v1").strip() or "v1",
        )


@dataclass(frozen=True)
class CanaryAssignment:
    mode: str
    candidate_id: str
    reason: str
    policy_version: str
    bucket: int | None = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "candidate_id": self.candidate_id,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "bucket": self.bucket,
        }


def _stable(*, reason: str, policy_version: str, bucket: int | None = None) -> CanaryAssignment:
    return CanaryAssignment(
        mode=MODE_STABLE, candidate_id="", reason=reason, policy_version=policy_version, bucket=bucket
    )


def assign_canary(
    *, tenant_id: str, session_id: str = "", config: CanaryConfig | None = None
) -> CanaryAssignment:
    """Deterministic/sticky stable-vs-candidate assignment.

    Same ``(tenant_id, session_id, candidate_id, policy_version)`` always
    yields the same bucket/mode -- required for tenant/session routing
    consistency. Fails safe (STABLE) for any ambiguous/invalid config.
    """

    cfg = config or CanaryConfig()
    if not cfg.is_usable():
        return _stable(reason="canary_disabled_or_invalid_config", policy_version=cfg.policy_version)

    tid = str(tenant_id or "").strip()
    if not tid:
        # Cannot form a stable identity key -- fail safe rather than bucket
        # anonymously (would break per-tenant stickiness guarantees).
        return _stable(reason="missing_tenant_identity", policy_version=cfg.policy_version)

    identity = f"{tid}:{session_id or tid}"
    bucket = stable_bucket(
        identity_key=identity, candidate_id=cfg.candidate_id, policy_version=cfg.policy_version
    )
    if bucket < cfg.percent_basis_points:
        return CanaryAssignment(
            mode=MODE_CANDIDATE,
            candidate_id=cfg.candidate_id,
            reason="percentage_bucket",
            policy_version=cfg.policy_version,
            bucket=bucket,
        )
    return _stable(reason="percentage_bucket_miss", policy_version=cfg.policy_version, bucket=bucket)


def _record_assignment_metric(assignment: CanaryAssignment) -> None:
    try:
        from runtime.metrics import RUNTIME_COUNTERS

        name = "canary_candidate" if assignment.mode == MODE_CANDIDATE else "canary_stable"
        RUNTIME_COUNTERS.inc(name)
    except Exception:
        pass


@dataclass(frozen=True)
class RollbackRecord:
    """Observable, auditable rollback event (Scale 3.38)."""

    triggered: bool
    trigger_type: str  # manual | automatic
    reason: str
    actor: str
    rolled_back_at: datetime | None
    already_rolled_back: bool

    def as_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "trigger_type": self.trigger_type,
            "reason": self.reason,
            "actor": self.actor,
            "rolled_back_at": self.rolled_back_at.isoformat() if self.rolled_back_at else None,
            "already_rolled_back": self.already_rolled_back,
        }


@dataclass
class CanaryRolloutController:
    """Trusted runtime facade: assignment + manual/automatic rollback.

    Process-local rollback state is intentionally simple (a boolean latch)
    because rollback is a rare, operator/health-triggered event, not a
    high-frequency distributed counter; each instance independently
    latching rollback on its own detection of a breach is itself a safe,
    conservative behavior (a false negative in one instance never sends
    MORE candidate traffic than configured; at worst a slow-to-detect
    instance keeps routing its own share of traffic per the last-known-good
    config until it also observes the breach or is redeployed with rollback
    baked into ``CanaryConfig``).
    """

    config: CanaryConfig = field(default_factory=CanaryConfig)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _rolled_back: bool = field(default=False, repr=False)
    _rollback_reason: str = field(default="", repr=False)
    _rollback_trigger: str = field(default="", repr=False)
    _rollback_actor: str = field(default="", repr=False)
    _rollback_at: datetime | None = field(default=None, repr=False)
    _consecutive_breaches: int = field(default=0, repr=False)

    def is_rolled_back(self) -> bool:
        with self._lock:
            return self._rolled_back

    def assign(self, *, tenant_id: str, session_id: str = "") -> CanaryAssignment:
        if self.is_rolled_back():
            assignment = _stable(reason="rolled_back", policy_version=self.config.policy_version)
        else:
            assignment = assign_canary(tenant_id=tenant_id, session_id=session_id, config=self.config)
        _record_assignment_metric(assignment)
        return assignment

    def _do_rollback(
        self, *, reason: str, actor: str, trigger_type: str, now: datetime | None = None
    ) -> RollbackRecord:
        stamp = now or utc_now()
        with self._lock:
            already = self._rolled_back
            if not already:
                self._rolled_back = True
                self._rollback_reason = str(reason or "rollback")
                self._rollback_trigger = trigger_type
                self._rollback_actor = str(actor or "")
                self._rollback_at = stamp
            record = RollbackRecord(
                triggered=not already,
                trigger_type=self._rollback_trigger or trigger_type,
                reason=self._rollback_reason,
                actor=self._rollback_actor,
                rolled_back_at=self._rollback_at,
                already_rolled_back=already,
            )
        try:
            from runtime.metrics import RUNTIME_COUNTERS

            RUNTIME_COUNTERS.inc("canary_rollback")
        except Exception:
            pass
        return record

    def rollback(
        self, *, reason: str, actor: str = "", now: datetime | None = None
    ) -> RollbackRecord:
        """Manual rollback: idempotent, safe under repeated invocation.

        Never deletes historical telemetry; only latches new authoritative
        assignment to STABLE going forward.
        """

        return self._do_rollback(
            reason=reason, actor=actor, trigger_type=ROLLBACK_MANUAL, now=now
        )

    def reset_rollback(self, *, actor: str = "") -> None:
        """Administrative recovery: explicitly clear a prior rollback latch.

        Never automatic -- resuming candidate traffic after a rollback is
        always an explicit trusted operator action.
        """

        with self._lock:
            self._rolled_back = False
            self._rollback_reason = ""
            self._rollback_trigger = ""
            self._rollback_actor = str(actor or "")
            self._rollback_at = None
            self._consecutive_breaches = 0

    def evaluate_automatic_rollback(
        self,
        *,
        candidate_error_rate: float,
        max_error_rate: float = 0.10,
        breach_streak_required: int = 3,
        now: datetime | None = None,
    ) -> RollbackRecord | None:
        """Bounded, trusted, debounced automatic rollback decision.

        Requires ``breach_streak_required`` consecutive evaluations above
        ``max_error_rate`` before triggering -- avoids flapping on a single
        noisy sample. Returns ``None`` when no rollback decision was made
        this call (streak reset or already rolled back); returns a
        ``RollbackRecord`` exactly when this call causes (or confirms) a
        rollback.
        """

        if self.is_rolled_back():
            return None
        with self._lock:
            if candidate_error_rate > max_error_rate:
                self._consecutive_breaches += 1
            else:
                self._consecutive_breaches = 0
                return None
            if self._consecutive_breaches < breach_streak_required:
                return None
        reason = f"candidate_error_rate_breach:{candidate_error_rate:.4f}>{max_error_rate:.4f}"
        return self._do_rollback(
            reason=reason, actor="automatic_rollback", trigger_type=ROLLBACK_AUTOMATIC, now=now
        )
