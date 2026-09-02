"""Policy envelope evaluation and kill switch."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from controlled_automation.errors import ACTION_NOT_ALLOWED, KILL_SWITCH_ACTIVE, POLICY_DENIED, RATE_LIMITED
from controlled_automation.models import ALLOWED_ACTIONS, PolicyEnvelope


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class KillSwitchRegistry:
    def __init__(self):
        self._global = False
        self._tenant: set[str] = set()
        self._automation: set[tuple[str, str]] = set()
        self._integration: set[tuple[str, str]] = set()
        self._action_type: set[tuple[str, str]] = set()

    def activate(self, *, scope: str, tenant_id: str | None = None, ref: str | None = None) -> None:
        if scope == "GLOBAL":
            self._global = True
        elif scope == "TENANT" and tenant_id:
            self._tenant.add(tenant_id)
        elif scope == "AUTOMATION" and tenant_id and ref:
            self._automation.add((tenant_id, ref))
        elif scope == "INTEGRATION" and tenant_id and ref:
            self._integration.add((tenant_id, ref))
        elif scope == "ACTION_TYPE" and tenant_id and ref:
            self._action_type.add((tenant_id, ref))

    def is_blocked(
        self,
        *,
        tenant_id: str,
        automation_id: str | None = None,
        integration_id: str | None = None,
        action_type: str | None = None,
    ) -> str | None:
        if self._global:
            return "GLOBAL"
        if tenant_id in self._tenant:
            return "TENANT"
        if automation_id and (tenant_id, automation_id) in self._automation:
            return "AUTOMATION"
        if integration_id and (tenant_id, integration_id) in self._integration:
            return "INTEGRATION"
        if action_type and (tenant_id, action_type) in self._action_type:
            return "ACTION_TYPE"
        return None


class PolicyEvaluator:
    def __init__(self, *, kill_switch: KillSwitchRegistry | None = None):
        self.kill_switch = kill_switch or KillSwitchRegistry()
        self._hourly: dict[tuple[str, str], list[str]] = {}
        self._daily: dict[tuple[str, str], list[str]] = {}

    def evaluate(
        self,
        *,
        tenant_id: str,
        automation_id: str,
        policy: PolicyEnvelope,
        actions: tuple[dict[str, Any], ...],
        now_iso: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        blocked = self.kill_switch.is_blocked(tenant_id=tenant_id, automation_id=automation_id)
        if blocked:
            return {"allowed": False, "code": KILL_SWITCH_ACTIVE, "scope": blocked}

        if policy.valid_from and now_iso < policy.valid_from:
            return {"allowed": False, "code": POLICY_DENIED, "reason": "not_yet_valid"}
        if policy.valid_until and now_iso > policy.valid_until:
            return {"allowed": False, "code": POLICY_DENIED, "reason": "expired"}

        if len(actions) > policy.max_actions_per_run:
            return {"allowed": False, "code": POLICY_DENIED, "reason": "max_actions_per_run"}

        for action in actions:
            at = str(action.get("action_type") or "")
            iid = str(action.get("integration_id") or "")
            blocked = self.kill_switch.is_blocked(
                tenant_id=tenant_id,
                automation_id=automation_id,
                integration_id=iid or None,
                action_type=at or None,
            )
            if blocked:
                return {"allowed": False, "code": KILL_SWITCH_ACTIVE, "scope": blocked}
            if at not in ALLOWED_ACTIONS:
                return {"allowed": False, "code": ACTION_NOT_ALLOWED, "action": at}
            if policy.allowed_action_types and at not in policy.allowed_action_types:
                return {"allowed": False, "code": ACTION_NOT_ALLOWED, "action": at}
            iid = str(action.get("integration_id") or "")
            if policy.allowed_integration_ids and iid and iid not in policy.allowed_integration_ids:
                return {"allowed": False, "code": POLICY_DENIED, "reason": "integration_not_allowed"}
            resource = str(action.get("resource") or action.get("subject_id") or "")
            if policy.allowed_resource_scope and resource and resource not in policy.allowed_resource_scope:
                return {"allowed": False, "code": POLICY_DENIED, "reason": "resource_scope"}

        key = (tenant_id, automation_id)
        hour_key = now_iso[:13]
        day_key = now_iso[:10]
        hour_count = sum(1 for ts in self._hourly.get(key, []) if ts.startswith(hour_key))
        day_count = sum(1 for ts in self._daily.get(key, []) if ts.startswith(day_key))
        if not dry_run and hour_count >= policy.max_actions_per_hour:
            return {"allowed": False, "code": RATE_LIMITED, "reason": "hourly"}
        if not dry_run and day_count >= policy.max_actions_per_day:
            return {"allowed": False, "code": RATE_LIMITED, "reason": "daily"}

        return {"allowed": True, "dry_run": dry_run, "policy_dry_run": policy.dry_run}

    def record_execution(self, *, tenant_id: str, automation_id: str, now_iso: str) -> None:
        key = (tenant_id, automation_id)
        self._hourly.setdefault(key, []).append(now_iso)
        self._daily.setdefault(key, []).append(now_iso)
