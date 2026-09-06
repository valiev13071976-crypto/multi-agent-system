"""Live shadow traffic (Scale 3.36).

Shadow traffic mirrors selected production requests to a candidate path for
evaluation WITHOUT ever letting the shadow execution:

- replace the user-visible authoritative response, or
- produce an unauthorized business side effect.

Two independent safety layers enforce this contract:

1. **Type-level non-authority**: shadow execution here always returns a
   ``ShadowExecutionRecord`` -- a distinct, safe-summary-only type -- never a
   ``tools.models.ToolResult``. Callers cannot accidentally propagate a
   shadow outcome to a user because the return type itself is not a tool
   result.
2. **Side-effect firewall enforced at the tool capability layer itself**
   (``tools.gateway.ToolGateway.invoke``, Scale 3.36): any request flagged
   ``metadata["traffic_mode"] == "shadow"`` is rejected with
   ``ToolShadowNotEligibleError`` / ``SHADOW_NOT_ELIGIBLE`` unless the
   resolved tool descriptor is proven safe (``read_only`` or
   ``side_effect_level`` in {none, read}). This module additionally
   pre-checks eligibility so unsafe tools are never even attempted, but the
   gateway-level check is the one that cannot be bypassed by a caller
   forgetting to pre-check.

Sampling is deterministic (stable hash bucket) so tests can reproduce
sampled/unsampled outcomes without randomness. Default: OFF (0% sample rate).
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from tools.models import SIDE_EFFECT_NONE, SIDE_EFFECT_READ, ToolDescriptor, ToolRequest

TRAFFIC_MODE_SHADOW = "shadow"

OUTCOME_DISABLED = "disabled"
OUTCOME_NOT_SAMPLED = "not_sampled"
OUTCOME_INELIGIBLE = "ineligible"
OUTCOME_EXECUTED = "executed"
OUTCOME_ERROR = "error"

_VALID_OUTCOMES = frozenset(
    {OUTCOME_DISABLED, OUTCOME_NOT_SAMPLED, OUTCOME_INELIGIBLE, OUTCOME_EXECUTED, OUTCOME_ERROR}
)


@dataclass(frozen=True)
class ShadowTrafficConfig:
    """Bounded, typed, safe-default shadow policy. Default: OFF / 0%."""

    enabled: bool = False
    sample_rate: float = 0.0  # 0.0..1.0

    @classmethod
    def from_env(cls, env: Mapping | None = None) -> "ShadowTrafficConfig":
        source = env if env is not None else os.environ

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        def _ratio(name: str, default: float) -> float:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                value = float(str(raw).strip())
            except ValueError:
                return default
            if value < 0.0 or value > 1.0:
                return default
            return value

        return cls(
            enabled=_bool("SHADOW_TRAFFIC_ENABLED", False),
            sample_rate=_ratio("SHADOW_TRAFFIC_SAMPLE_RATE", 0.0),
        )


def is_shadow_eligible(descriptor: ToolDescriptor) -> bool:
    """A tool is shadow-safe only if it cannot produce a business side effect."""

    return bool(
        descriptor.read_only or descriptor.side_effect_level in {SIDE_EFFECT_NONE, SIDE_EFFECT_READ}
    )


def should_sample_shadow(sample_key: str, config: ShadowTrafficConfig) -> bool:
    """Deterministic sampling decision (no randomness -- reproducible in tests)."""

    if not config.enabled or config.sample_rate <= 0.0:
        return False
    digest = hashlib.sha256(f"shadow:{sample_key}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 10000
    return bucket < int(round(config.sample_rate * 10000))


@dataclass(frozen=True)
class ShadowExecutionRecord:
    """Non-authoritative diagnostic record of a shadow attempt.

    Never a ``ToolResult`` -- deliberately a distinct type so it cannot be
    mistaken for (or accidentally substituted as) the authoritative result.
    """

    tool_id: str
    outcome: str
    sampled: bool
    eligible: bool
    error_code: str | None = None
    status: str | None = None
    success: bool | None = None
    latency_ms: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.outcome not in _VALID_OUTCOMES:
            raise ValueError(f"invalid shadow outcome: {self.outcome!r}")

    def as_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "outcome": self.outcome,
            "sampled": self.sampled,
            "eligible": self.eligible,
            "error_code": self.error_code,
            "status": self.status,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "metadata": dict(self.metadata),
        }


def _record_metrics(record: ShadowExecutionRecord) -> None:
    try:
        from runtime.metrics import RUNTIME_COUNTERS

        name = "shadow_executed" if record.outcome == OUTCOME_EXECUTED else "shadow_blocked"
        RUNTIME_COUNTERS.inc(name)
    except Exception:
        pass


async def run_shadow_tool_call(
    gateway,
    base_request: ToolRequest,
    *,
    config: ShadowTrafficConfig | None = None,
    sample_key: str = "",
    **invoke_kwargs,
) -> ShadowExecutionRecord:
    """Attempt a non-authoritative shadow execution of ``base_request``.

    Never raises for ordinary ineligibility/sampling/disablement -- those are
    represented as an ``outcome`` on the returned record. The caller's
    authoritative response path is completely independent of this call.
    """

    cfg = config or ShadowTrafficConfig()
    tool_id = base_request.tool_id
    key = sample_key or base_request.tenant_id or base_request.request_id

    if not cfg.enabled:
        record = ShadowExecutionRecord(
            tool_id=tool_id, outcome=OUTCOME_DISABLED, sampled=False, eligible=False
        )
        _record_metrics(record)
        return record

    sampled = should_sample_shadow(key, cfg)
    if not sampled:
        record = ShadowExecutionRecord(
            tool_id=tool_id, outcome=OUTCOME_NOT_SAMPLED, sampled=False, eligible=False
        )
        _record_metrics(record)
        return record

    # Pre-flight eligibility check (defense-in-depth #1): never even attempt
    # invoke() for a tool that cannot possibly be shadow-safe.
    try:
        registration = gateway.registry.resolve(tool_id, base_request.tool_version or None)
        descriptor = registration.descriptor
    except Exception:
        record = ShadowExecutionRecord(
            tool_id=tool_id,
            outcome=OUTCOME_INELIGIBLE,
            sampled=True,
            eligible=False,
            error_code="tool_not_found",
        )
        _record_metrics(record)
        return record

    if not is_shadow_eligible(descriptor):
        record = ShadowExecutionRecord(
            tool_id=tool_id,
            outcome=OUTCOME_INELIGIBLE,
            sampled=True,
            eligible=False,
            error_code="SHADOW_NOT_ELIGIBLE",
        )
        _record_metrics(record)
        return record

    from dataclasses import replace

    shadow_meta = dict(base_request.metadata or {})
    shadow_meta["traffic_mode"] = TRAFFIC_MODE_SHADOW
    shadow_request = replace(
        base_request,
        request_id=f"shadow-{base_request.request_id}",
        metadata=shadow_meta,
    )

    started = time.monotonic()
    try:
        # Defense-in-depth #2: the gateway itself re-checks eligibility from
        # metadata["traffic_mode"] and fails closed even if this module's
        # pre-flight check above were ever bypassed or stale.
        result = await gateway.invoke(shadow_request, **invoke_kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)
        record = ShadowExecutionRecord(
            tool_id=tool_id,
            outcome=OUTCOME_EXECUTED,
            sampled=True,
            eligible=True,
            status=getattr(result, "status", None),
            success=getattr(result, "success", None),
            error_code=getattr(result, "error_code", None) or None,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        record = ShadowExecutionRecord(
            tool_id=tool_id,
            outcome=OUTCOME_ERROR,
            sampled=True,
            eligible=True,
            error_code=getattr(exc, "error_code", "shadow_execution_error"),
            latency_ms=latency_ms,
        )
    _record_metrics(record)
    return record
