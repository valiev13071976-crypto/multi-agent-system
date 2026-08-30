"""Deterministic cohort resolution and traffic policy."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace

from controlled_launch.models import TrafficDecision, TrafficMode, TrafficPolicy


def stable_bucket(*, identity_key: str, candidate_id: str, policy_version: str, modulus: int = 10000) -> int:
    digest = hashlib.sha256(f"{identity_key}:{candidate_id}:{policy_version}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulus


class CohortResolver:
    """Priority: exclusion → internal → test tenant → canary tenant/user → workload → percentage → control."""

    @staticmethod
    def resolve(
        policy: TrafficPolicy,
        *,
        request_id: str,
        tenant_id: str,
        user_id: str = "",
        workload_class: str = "",
    ) -> TrafficDecision:
        if policy.kill_switch or policy.abort:
            return _decision(
                request_id=request_id,
                policy=policy,
                mode=TrafficMode.CONTROL,
                control=True,
                candidate=False,
                cohort_id="excluded_kill_switch",
                reason="kill_switch_or_abort",
                workload_class=workload_class,
            )
        if tenant_id in policy.excluded_tenants:
            return _decision(
                request_id, policy, TrafficMode.CONTROL, True, False, "excluded_tenant", "exclusion", workload_class
            )
        identity = f"{tenant_id}:{user_id or tenant_id}"
        if tenant_id in policy.internal_tenants:
            return _decision(
                request_id,
                policy,
                TrafficMode.INTERNAL,
                False,
                True,
                "internal",
                "internal_tenant",
                workload_class,
                side_effect=policy.side_effect_allowed,
                billing=policy.billing_allowed,
            )
        if tenant_id in policy.test_tenants:
            return _decision(
                request_id, policy, TrafficMode.INTERNAL, False, True, "test_tenant", "test_tenant", workload_class
            )
        if tenant_id in policy.canary_tenants or user_id in policy.canary_users:
            return _decision(
                request_id,
                policy,
                TrafficMode.CANARY,
                False,
                True,
                "explicit_canary",
                "explicit_cohort",
                workload_class,
                side_effect=policy.side_effect_allowed,
                billing=policy.billing_allowed,
            )
        if workload_class and workload_class in policy.workload_cohorts:
            return _decision(
                request_id,
                policy,
                TrafficMode.CANARY,
                False,
                True,
                f"workload:{workload_class}",
                "workload_cohort",
                workload_class,
                side_effect=policy.side_effect_allowed,
                billing=policy.billing_allowed,
            )
        if policy.percent_basis_points > 0 and not policy.hold:
            bucket = stable_bucket(identity_key=identity, candidate_id=policy.candidate_id, policy_version=policy.policy_version)
            if bucket < policy.percent_basis_points:
                return _decision(
                    request_id,
                    policy,
                    TrafficMode.CANARY,
                    False,
                    True,
                    "percentage",
                    "percentage_bucket",
                    workload_class,
                    side_effect=policy.side_effect_allowed,
                    billing=policy.billing_allowed,
                )
        return _decision(request_id, policy, TrafficMode.CONTROL, True, False, "control", "default_control", workload_class)


def _decision(
    request_id: str,
    policy: TrafficPolicy,
    mode: TrafficMode,
    control: bool,
    candidate: bool,
    cohort_id: str,
    reason: str,
    workload_class: str,
    *,
    side_effect: bool = False,
    billing: bool = False,
) -> TrafficDecision:
    shadow = policy.shadow_enabled and control and not policy.abort
    return TrafficDecision(
        request_id=request_id,
        candidate_id=policy.candidate_id if candidate else "",
        mode=mode.value,
        control_target=control,
        candidate_target=candidate,
        cohort_id=cohort_id,
        assignment_reason=reason,
        workload_class=workload_class,
        shadow_allowed=shadow,
        side_effect_allowed=side_effect and candidate,
        billing_allowed=billing and candidate,
        policy_version=policy.policy_version,
    )


class TrafficPolicyFactory:
    @staticmethod
    def create(
        *,
        candidate_id: str,
        mode: TrafficMode,
        created_by: str,
        percent_basis_points: int = 0,
        **kwargs,
    ) -> TrafficPolicy:
        return TrafficPolicy(
            policy_id=f"tp-{uuid.uuid4().hex[:12]}",
            policy_version=f"v-{uuid.uuid4().hex[:8]}",
            candidate_id=candidate_id,
            mode=mode.value,
            percent_basis_points=max(0, min(10000, int(percent_basis_points))),
            created_by=created_by,
            **kwargs,
        )

    @staticmethod
    def with_kill_switch(policy: TrafficPolicy, *, enabled: bool) -> TrafficPolicy:
        return replace(policy, kill_switch=enabled)

    @staticmethod
    def with_hold(policy: TrafficPolicy, *, hold: bool) -> TrafficPolicy:
        return replace(policy, hold=hold)

    @staticmethod
    def with_abort(policy: TrafficPolicy, *, abort: bool) -> TrafficPolicy:
        return replace(policy, abort=abort)
