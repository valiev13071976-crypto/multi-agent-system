"""Controlled launch router — server-side traffic authority."""

from __future__ import annotations

from dataclasses import dataclass, field

from controlled_launch.errors import BLOCKED_BY_STAGE_3, TRAFFIC_DENIED, ControlledLaunchError
from controlled_launch.handoff import Stage3HandoffGate
from controlled_launch.models import LaunchCandidate, TrafficDecision, TrafficMode, TrafficPolicy
from controlled_launch.traffic_policy import CohortResolver
from evals.canary import CanaryController


@dataclass
class ControlledLaunchRouter:
    handoff_gate: Stage3HandoffGate | None = None
    canary: CanaryController = field(default_factory=CanaryController)
    _client_overrides_blocked: bool = True

    def __post_init__(self):
        if self.handoff_gate is None:
            self.handoff_gate = Stage3HandoffGate()

    def reject_client_authority(self, payload: dict | None) -> None:
        if not payload:
            return
        forbidden = (
            "mode",
            "candidate_id",
            "percent",
            "traffic_percent",
            "cohort_id",
            "side_effect_allowed",
            "billing_allowed",
            "provider",
            "model",
        )
        for key in forbidden:
            if key in payload:
                raise ControlledLaunchError(TRAFFIC_DENIED, details={"client_forbidden_key": key})

    def decide(
        self,
        *,
        request_id: str,
        tenant_id: str,
        user_id: str = "",
        workload_class: str = "",
        policy: TrafficPolicy,
        candidate: LaunchCandidate | None = None,
        client_payload: dict | None = None,
        allow_live: bool = False,
    ) -> TrafficDecision:
        self.reject_client_authority(client_payload)
        live_modes = {TrafficMode.INTERNAL.value, TrafficMode.SHADOW.value, TrafficMode.CANARY.value, TrafficMode.LIMITED.value}
        if policy.mode in live_modes and not allow_live:
            if not self.handoff_gate.allows_live_traffic():
                raise ControlledLaunchError(BLOCKED_BY_STAGE_3)
        decision = CohortResolver.resolve(
            policy,
            request_id=request_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workload_class=workload_class,
        )
        if decision.candidate_target and candidate is not None:
            if candidate.status in {"ABORTED", "ROLLED_BACK"}:
                return CohortResolver.resolve(
                    TrafficPolicy(
                        policy_id=policy.policy_id,
                        policy_version=policy.policy_version,
                        candidate_id="",
                        mode=TrafficMode.CONTROL.value,
                        abort=True,
                    ),
                    request_id=request_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workload_class=workload_class,
                )
        return decision
