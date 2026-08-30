"""Stage-4 controlled-launch admission over ControlledLaunchPolicy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from controlled_launch.policy import ControlledLaunchPolicy


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason_code: str
    policy_version: str
    release_identity: str
    tenant_id: str
    user_id: str
    workload_class: str
    timestamp: str

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "reason_code": self.reason_code,
            "policy_version": self.policy_version,
            "release_identity": self.release_identity,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "workload_class": self.workload_class,
            "timestamp": self.timestamp,
        }


class ControlledLaunchAdmission:
    """Fail-closed cohort admission. Does not replace auth/RBAC."""

    def decide(
        self,
        policy: ControlledLaunchPolicy | None,
        *,
        tenant_id: str,
        user_id: str = "",
        workload_class: str = "interactive",
        authenticated: bool = False,
        authorized: bool = False,
        active_cohort_count: int = 0,
        active_interactive: int = 0,
        active_batch: int = 0,
        tenant_active: int = 0,
        spent: float = 0.0,
    ) -> AdmissionDecision:
        now = datetime.now(timezone.utc).isoformat()
        if policy is None:
            return AdmissionDecision(False, "policy_missing", "", "", tenant_id, user_id, workload_class, now)
        if policy.kill_switch:
            return AdmissionDecision(False, "kill_switch_active", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if not policy.enabled:
            return AdmissionDecision(False, "launch_disabled", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if not authenticated:
            return AdmissionDecision(False, "unauthenticated", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if not authorized:
            return AdmissionDecision(False, "unauthorized", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if policy.containment_action in {"PAUSE_ADMISSION", "KILL_CONTROLLED_LAUNCH"}:
            return AdmissionDecision(False, f"containment_{policy.containment_action.lower()}", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        in_cohort = False
        if tenant_id and tenant_id in policy.tenant_allowlist:
            in_cohort = True
        if user_id and user_id in policy.user_allowlist:
            in_cohort = True
        if not in_cohort:
            return AdmissionDecision(False, "not_in_cohort", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if policy.max_cohort_size and active_cohort_count >= policy.max_cohort_size:
            return AdmissionDecision(False, "cohort_limit", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if workload_class == "interactive" and policy.max_interactive_concurrency and active_interactive >= policy.max_interactive_concurrency:
            return AdmissionDecision(False, "interactive_concurrency_limit", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if workload_class in {"batch", "background"} and policy.max_batch_concurrency and active_batch >= policy.max_batch_concurrency:
            return AdmissionDecision(False, "batch_concurrency_limit", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if policy.per_tenant_concurrency and tenant_active >= policy.per_tenant_concurrency:
            return AdmissionDecision(False, "tenant_concurrency_limit", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        if policy.budget_ceiling and spent >= policy.budget_ceiling:
            return AdmissionDecision(False, "budget_ceiling", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)
        return AdmissionDecision(True, "admitted", policy.policy_version, policy.release_identity, tenant_id, user_id, workload_class, now)

    def capability_allowed(self, policy: ControlledLaunchPolicy | None, capability: str) -> bool:
        if policy is None:
            return False
        return capability not in policy.restricted_capabilities
