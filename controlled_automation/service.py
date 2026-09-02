"""Controlled automation orchestration service."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from controlled_automation.access import (
    PERM_AUTOMATION_CREATE,
    PERM_AUTOMATION_ENABLE,
    PERM_AUTOMATION_READ,
    PERM_AUTOMATION_RUN,
    PERM_AUTOMATION_UPDATE,
    ControlledAutomationAccessPolicy,
)
from controlled_automation.conditions import evaluate_condition
from controlled_automation.config import controlled_automation_expansion_live_active
from controlled_automation.dispatcher import ControlledAutomationDispatcher
from controlled_automation.errors import (
    ACTION_NOT_ALLOWED,
    APPROVAL_EXPIRED,
    APPROVAL_REJECTED,
    APPROVAL_REQUIRED,
    APPROVAL_STALE,
    AUTOMATION_NOT_FOUND,
    BUDGET_EXCEEDED,
    CAPABILITY_DENIED,
    INVALID_AUTOMATION,
    KILL_SWITCH_ACTIVE,
    LIVE_FALLBACK_FORBIDDEN,
    OVERLAP_BLOCKED,
    POLICY_DENIED,
    RISK_HITL_REQUIRED,
    STALE_VERSION,
    ControlledAutomationError,
)
from controlled_automation.events import BusinessEventStore
from controlled_automation.models import (
    ALLOWED_ACTIONS,
    FORBIDDEN_PAYLOAD_KEYS,
    RUN_BLOCKED,
    RUN_EVALUATING,
    RUN_FAILED,
    RUN_NO_ACTION,
    RUN_PREPARED,
    RUN_SUCCEEDED,
    RUN_UNKNOWN_EXTERNAL,
    RUN_VERIFYING,
    RUN_WAITING_APPROVAL,
    STATE_ARCHIVED,
    STATE_DISABLED,
    STATE_DRAFT,
    STATE_ENABLED,
    STATE_PAUSED,
    TRIGGER_BUSINESS_EVENT,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    AutomationRun,
    ControlledAutomationDefinition,
    PolicyEnvelope,
)
from controlled_automation.observability import ControlledAutomationObservability
from controlled_automation.policy import KillSwitchRegistry, PolicyEvaluator
from controlled_automation.risk import can_auto_execute, default_risk_for_action, requires_hitl
from controlled_automation.store import ControlledAutomationStore, InMemoryControlledAutomationStore
from security.identity import RequestSecurityContext
from security.tenant import require_tenant_id


class ControlledAutomationService:
    def __init__(
        self,
        *,
        store: ControlledAutomationStore | None = None,
        dispatcher: ControlledAutomationDispatcher | None = None,
        access: ControlledAutomationAccessPolicy | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
        event_store: BusinessEventStore | None = None,
        capability_checker: Callable[[str, tuple[str, ...]], bool] | None = None,
        budget_checker: Callable[[str, dict[str, Any]], bool] | None = None,
        facts_provider: Callable[[str, ControlledAutomationDefinition, dict[str, Any]], dict[str, Any]] | None = None,
        obs: ControlledAutomationObservability | None = None,
        kill_switch: KillSwitchRegistry | None = None,
    ):
        self._store = store or InMemoryControlledAutomationStore()
        self._dispatcher = dispatcher or ControlledAutomationDispatcher()
        self._access = access or ControlledAutomationAccessPolicy()
        self._policy = policy_evaluator or PolicyEvaluator(kill_switch=kill_switch or KillSwitchRegistry())
        self._events = event_store or BusinessEventStore()
        self._capability_checker = capability_checker or (lambda tenant, caps: True)
        self._budget_checker = budget_checker or (lambda tenant, meta: True)
        self._facts_provider = facts_provider or (lambda tenant, definition, ctx: dict(ctx.get("facts") or {}))
        self._obs = obs or ControlledAutomationObservability()
        self._running: dict[tuple[str, str], str] = {}
        self._last_run_at: dict[tuple[str, str], str] = {}
        self._approvals: dict[str, dict[str, Any]] = {}

    @property
    def store(self) -> ControlledAutomationStore:
        return self._store

    @property
    def kill_switch(self) -> KillSwitchRegistry:
        return self._policy.kill_switch

    @property
    def observability(self) -> ControlledAutomationObservability:
        return self._obs

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def create(self, ctx: RequestSecurityContext, payload: dict[str, Any]) -> dict[str, Any]:
        tenant = require_tenant_id(str(payload.get("tenant_id") or ctx.tenant_id))
        self._access.require(ctx, PERM_AUTOMATION_CREATE, tenant_id=tenant)
        if controlled_automation_expansion_live_active():
            raise ControlledAutomationError(LIVE_FALLBACK_FORBIDDEN, "live_not_implemented")
        self._validate_payload(payload)
        now = self._now_iso()
        policy = self._policy_from_payload(payload.get("policy") or {})
        actions = tuple(payload.get("actions") or ())
        risk = str(payload.get("risk_class") or default_risk_for_action(str((actions[0] or {}).get("action_type") if actions else "ANALYTICS_READ")))
        definition = ControlledAutomationDefinition(
            automation_id=str(payload.get("automation_id") or uuid.uuid4()),
            tenant_id=tenant,
            owner_id=str(payload.get("owner_id") or ctx.user_id or "owner"),
            name=str(payload.get("name") or "automation"),
            description=str(payload.get("description") or ""),
            enabled=bool(payload.get("enabled", False)),
            paused=False,
            state=STATE_DRAFT if not payload.get("enabled") else STATE_ENABLED,
            version=1,
            trigger=dict(payload.get("trigger") or {"type": TRIGGER_MANUAL}),
            conditions=dict(payload.get("conditions") or {"op": "ALL", "conditions": []}),
            actions=actions,
            policy=policy,
            risk_class=risk,
            approval_policy=dict(payload.get("approval_policy") or {}),
            budget_policy=dict(payload.get("budget_policy") or {}),
            rate_policy=dict(payload.get("rate_policy") or {}),
            scope=dict(payload.get("scope") or {}),
            required_capabilities=tuple(payload.get("required_capabilities") or ()),
            schedule_id=payload.get("schedule_id"),
            created_at=now,
            updated_at=now,
            created_by=str(ctx.user_id or "system"),
            metadata=dict(payload.get("metadata") or {}),
        )
        if definition.enabled and risk in {"R3_EXTERNAL_BUSINESS_WRITE", "R4_HIGH_IMPACT"}:
            definition = replace(definition, enabled=False, state=STATE_DRAFT)
        self._store.create(definition)
        self._store.append_audit(tenant_id=tenant, automation_id=definition.automation_id, event_type="created", payload={"version": 1})
        return self._def_dict(definition)

    def get(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_AUTOMATION_READ, tenant_id=tenant)
        d = self._store.get(tenant_id=tenant, automation_id=automation_id)
        if d is None:
            raise ControlledAutomationError(AUTOMATION_NOT_FOUND, automation_id)
        return self._def_dict(d)

    def list(self, ctx: RequestSecurityContext, *, tenant_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_AUTOMATION_READ, tenant_id=tenant)
        items = [self._def_dict(d) for d in self._store.list(tenant_id=tenant, limit=limit, offset=offset)]
        return {"tenant_id": tenant, "items": items, "mode": "FIXTURE"}

    def update(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str, patch: dict[str, Any], expected_version: int) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_AUTOMATION_UPDATE, tenant_id=tenant)
        current = self._store.get(tenant_id=tenant, automation_id=automation_id)
        if current is None:
            raise ControlledAutomationError(AUTOMATION_NOT_FOUND, automation_id)
        if current.version != expected_version:
            raise ControlledAutomationError(STALE_VERSION, "version_mismatch")
        merged = {**current.__dict__, **patch, "version": current.version + 1, "updated_at": self._now_iso()}
        if "policy" in patch:
            merged["policy"] = self._policy_from_payload(patch["policy"])
        if "actions" in patch:
            merged["actions"] = tuple(patch["actions"])
        self._validate_payload(merged, partial=True)
        updated = ControlledAutomationDefinition(**merged)
        self._store.update(updated, expected_version=expected_version)
        self._store.append_audit(tenant_id=tenant, automation_id=automation_id, event_type="updated", payload={"version": updated.version})
        return self._def_dict(updated)

    def enable(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str) -> dict[str, Any]:
        return self._set_state(ctx, tenant_id=tenant_id, automation_id=automation_id, enabled=True, paused=False, state=STATE_ENABLED)

    def pause(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str) -> dict[str, Any]:
        return self._set_state(ctx, tenant_id=tenant_id, automation_id=automation_id, enabled=True, paused=True, state=STATE_PAUSED)

    def resume(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str) -> dict[str, Any]:
        return self._set_state(ctx, tenant_id=tenant_id, automation_id=automation_id, enabled=True, paused=False, state=STATE_ENABLED)

    def disable(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str) -> dict[str, Any]:
        return self._set_state(ctx, tenant_id=tenant_id, automation_id=automation_id, enabled=False, paused=False, state=STATE_DISABLED)

    def dry_run(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._evaluate_and_execute(ctx, tenant_id=tenant_id, automation_id=automation_id, trigger_type=TRIGGER_MANUAL, dry_run=True, context=context or {})

    def run_now(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self._access.require(ctx, PERM_AUTOMATION_RUN, tenant_id=require_tenant_id(tenant_id))
        return self._evaluate_and_execute(ctx, tenant_id=tenant_id, automation_id=automation_id, trigger_type=TRIGGER_MANUAL, dry_run=False, context=context or {}, manual=True)

    def process_event(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str, event_payload: dict[str, Any]) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        try:
            ev = self._events.ingest(tenant_id=tenant, payload=event_payload)
        except ValueError as exc:
            raise ControlledAutomationError(INVALID_AUTOMATION, str(exc)) from exc
        return self._evaluate_and_execute(
            ctx,
            tenant_id=tenant,
            automation_id=automation_id,
            trigger_type=TRIGGER_BUSINESS_EVENT,
            dry_run=False,
            context={"event_id": ev.event_id, "facts": event_payload.get("facts") or {}},
        )

    def approve(self, ctx: RequestSecurityContext, *, tenant_id: str, run_id: str, approval_id: str, fingerprint: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        run = self._store.get_run(tenant_id=tenant, run_id=run_id)
        if run is None:
            raise ControlledAutomationError(AUTOMATION_NOT_FOUND, run_id)
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ControlledAutomationError(APPROVAL_REQUIRED, "missing")
        if approval.get("tenant_id") != tenant:
            raise ControlledAutomationError(APPROVAL_REJECTED, "tenant_mismatch")
        if approval.get("status") == "REJECTED":
            raise ControlledAutomationError(APPROVAL_REJECTED, "rejected")
        if approval.get("expires_at") and self._now_iso() > approval["expires_at"]:
            raise ControlledAutomationError(APPROVAL_EXPIRED, "expired")
        if approval.get("fingerprint") != fingerprint or run.approval_fingerprint != fingerprint:
            raise ControlledAutomationError(APPROVAL_STALE, "fingerprint_mismatch")
        approval["status"] = "APPROVED"
        definition = self._store.get(tenant_id=tenant, automation_id=run.automation_id)
        if definition is None:
            raise ControlledAutomationError(AUTOMATION_NOT_FOUND, run.automation_id)
        results = self._dispatcher.dispatch_actions(
            tenant_id=tenant,
            automation_id=run.automation_id,
            run_id=run.run_id,
            actions=run.actions_planned,
            dry_run=False,
            execution_key=run.execution_key,
        )
        run = replace(run, status=RUN_SUCCEEDED, actions_executed=tuple(r.__dict__ for r in results), completed_at=self._now_iso())
        self._store.save_run(run)
        self._policy.record_execution(tenant_id=tenant, automation_id=run.automation_id, now_iso=self._now_iso())
        self._running.pop((tenant, run.automation_id), None)
        self._obs.emit(event="run_succeeded", run_id=run_id)
        return {"run": run.__dict__, "results": [r.__dict__ for r in results]}

    def reject(self, ctx: RequestSecurityContext, *, tenant_id: str, run_id: str, approval_id: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        approval = self._approvals.get(approval_id)
        if approval:
            approval["status"] = "REJECTED"
        run = self._store.get_run(tenant_id=tenant, run_id=run_id)
        if run:
            run = replace(run, status=RUN_FAILED, error_code=APPROVAL_REJECTED, completed_at=self._now_iso())
            self._store.save_run(run)
        self._running.pop((tenant, run.automation_id if run else ""), None)
        return {"status": APPROVAL_REJECTED, "side_effects": 0}

    def list_runs(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str, limit: int = 50) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_AUTOMATION_READ, tenant_id=tenant)
        runs = self._store.list_runs(tenant_id=tenant, automation_id=automation_id, limit=limit)
        return {"tenant_id": tenant, "automation_id": automation_id, "runs": [r.__dict__ for r in runs]}

    def analytics_snapshot(self, *, tenant_id: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        items = self._store.list(tenant_id=tenant, limit=1000)
        enabled = [d for d in items if d.enabled and not d.paused]
        return {
            "tenant_id": tenant,
            "total_automations": len(items),
            "enabled_automations": len(enabled),
            "paused_automations": len([d for d in items if d.paused]),
            **self._obs.metrics_snapshot(total=len(items), enabled=len(enabled), blocked=len([d for d in items if d.state == "BLOCKED"])),
            "mode": "FIXTURE",
        }

    def ba_create_draft(self, *, tenant_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "draft": {
                "name": intent.get("name") or "Automation draft",
                "trigger": intent.get("trigger") or {"type": TRIGGER_SCHEDULE},
                "conditions": intent.get("conditions") or {"op": "ALL", "conditions": [{"field": "stock", "operator": "LT", "value": 10}]},
                "actions": intent.get("actions") or [{"action_type": "STOCK_READ", "resource": "default"}],
                "policy": {"dry_run": True, "allow_auto_execute": False, "requires_approval": True},
                "risk_class": "R1_PREPARE_ONLY",
                "enabled": False,
            },
            "mutation": False,
            "high_risk_auto_enable_blocked": True,
        }

    def ba_explain(self, *, tenant_id: str, automation_id: str) -> dict[str, Any]:
        d = self._store.get(tenant_id=tenant_id, automation_id=automation_id)
        if d is None:
            raise ControlledAutomationError(AUTOMATION_NOT_FOUND, automation_id)
        return {
            "automation_id": automation_id,
            "name": d.name,
            "risk_class": d.risk_class,
            "policy": {
                "requires_approval": d.policy.requires_approval or requires_hitl(risk_class=d.risk_class, allow_auto_execute=d.policy.allow_auto_execute),
                "dry_run": d.policy.dry_run,
                "allowed_actions": list(d.policy.allowed_action_types or ALLOWED_ACTIONS),
            },
            "state": d.state,
            "mode": "FIXTURE",
        }

    def _evaluate_and_execute(
        self,
        ctx: RequestSecurityContext,
        *,
        tenant_id: str,
        automation_id: str,
        trigger_type: str,
        dry_run: bool,
        context: dict[str, Any],
        manual: bool = False,
    ) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        definition = self._store.get(tenant_id=tenant, automation_id=automation_id)
        if definition is None:
            raise ControlledAutomationError(AUTOMATION_NOT_FOUND, automation_id)
        if definition.archived or definition.state == STATE_ARCHIVED:
            raise ControlledAutomationError(POLICY_DENIED, "archived")
        if definition.paused or definition.state == STATE_PAUSED:
            raise ControlledAutomationError(POLICY_DENIED, "paused")
        if not definition.enabled and not dry_run and not manual:
            raise ControlledAutomationError(POLICY_DENIED, "disabled")

        if self._running.get((tenant, automation_id)) and definition.policy.max_actions_per_run:
            raise ControlledAutomationError(OVERLAP_BLOCKED, "overlap_forbid")

        if not self._capability_checker(tenant, definition.required_capabilities):
            raise ControlledAutomationError(CAPABILITY_DENIED, "capability_denied")

        if not dry_run and not self._budget_checker(tenant, {"automation_id": automation_id}):
            raise ControlledAutomationError(BUDGET_EXCEEDED, "budget_denied")

        now = self._now_iso()
        run_id = str(uuid.uuid4())
        execution_key = f"controlled-automation:{automation_id}:v{definition.version}:{hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()[:16]}"
        if self._store.get_run(tenant_id=tenant, run_id=run_id) is None:
            existing = [r for r in self._store.list_runs(tenant_id=tenant, automation_id=automation_id, limit=100) if r.execution_key == execution_key and r.status == RUN_SUCCEEDED]
            if existing:
                return {"run": existing[0].__dict__, "status": "idempotent"}

        last_at = self._last_run_at.get((tenant, automation_id))
        if not dry_run and last_at and definition.policy.cooldown_seconds > 0:
            try:
                last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
                elapsed = (now_dt - last_dt).total_seconds()
                if elapsed < definition.policy.cooldown_seconds:
                    raise ControlledAutomationError(POLICY_DENIED, "cooldown_active")
            except ValueError:
                pass

        run = AutomationRun(
            run_id=run_id,
            automation_id=automation_id,
            tenant_id=tenant,
            automation_version=definition.version,
            trigger_type=trigger_type,
            event_id=context.get("event_id"),
            status=RUN_EVALUATING,
            execution_key=execution_key,
            dry_run=dry_run,
            started_at=now,
            trace_id=str(ctx.request_id or uuid.uuid4()),
        )
        self._store.save_run(run)

        facts = self._facts_provider(tenant, definition, context)
        cond = evaluate_condition(definition.conditions, facts=facts)
        run = replace(run, condition_result=cond)
        if not cond.get("satisfied"):
            status = RUN_NO_ACTION if cond.get("quality") == "KNOWN" else RUN_BLOCKED
            run = replace(run, status=status, blocked_reason=cond.get("reason") or cond.get("quality"), completed_at=self._now_iso())
            self._store.save_run(run)
            self._obs.emit(event="run_blocked", run_id=run_id, reason=run.blocked_reason)
            return {"run": run.__dict__, "condition": cond}

        policy_result = self._policy.evaluate(
            tenant_id=tenant,
            automation_id=automation_id,
            policy=definition.policy,
            actions=definition.actions,
            now_iso=now,
            dry_run=dry_run or definition.policy.dry_run,
        )
        run = replace(run, policy_result=policy_result)
        if not policy_result.get("allowed"):
            run = replace(run, status=RUN_BLOCKED, blocked_reason=policy_result.get("code"), completed_at=self._now_iso())
            self._store.save_run(run)
            self._obs.emit(event="run_blocked", run_id=run_id, reason=policy_result.get("code"))
            return {"run": run.__dict__, "policy": policy_result}

        effective_dry = dry_run or definition.policy.dry_run or policy_result.get("dry_run")
        needs_hitl = definition.policy.requires_approval or requires_hitl(risk_class=definition.risk_class, allow_auto_execute=definition.policy.allow_auto_execute)
        if not effective_dry and needs_hitl and not can_auto_execute(risk_class=definition.risk_class, allow_auto_execute=definition.policy.allow_auto_execute, dry_run=False):
            fp = hashlib.sha256(json.dumps({"actions": definition.actions, "version": definition.version}, sort_keys=True, default=str).encode()).hexdigest()
            approval_id = str(uuid.uuid4())
            self._approvals[approval_id] = {
                "approval_id": approval_id,
                "tenant_id": tenant,
                "automation_id": automation_id,
                "run_id": run_id,
                "fingerprint": fp,
                "status": "PENDING",
                "expires_at": None,
            }
            run = replace(
                run,
                status=RUN_WAITING_APPROVAL,
                actions_planned=definition.actions,
                approval_id=approval_id,
                approval_fingerprint=fp,
                completed_at=self._now_iso(),
            )
            self._store.save_run(run)
            self._obs.emit(event="waiting_approval", run_id=run_id)
            return {"run": run.__dict__, "approval_id": approval_id, "fingerprint": fp}

        self._running[(tenant, automation_id)] = run_id
        results = self._dispatcher.dispatch_actions(
            tenant_id=tenant,
            automation_id=automation_id,
            run_id=run_id,
            actions=definition.actions,
            dry_run=effective_dry,
            execution_key=execution_key,
        )
        if effective_dry:
            status = RUN_PREPARED
        else:
            status = RUN_SUCCEEDED
        run = replace(run, status=status, actions_planned=definition.actions, actions_executed=tuple(r.__dict__ for r in results), completed_at=self._now_iso())
        self._store.save_run(run)
        if not effective_dry:
            self._policy.record_execution(tenant_id=tenant, automation_id=automation_id, now_iso=now)
            self._last_run_at[(tenant, automation_id)] = now
        self._running.pop((tenant, automation_id), None)
        updated = replace(definition, last_evaluated_at=now, last_executed_at=now if not effective_dry else definition.last_executed_at, updated_at=now)
        self._store.update(updated, expected_version=definition.version)
        self._obs.emit(event="run_succeeded" if status == RUN_SUCCEEDED else "dry_run", run_id=run_id)
        return {"run": run.__dict__, "results": [r.__dict__ for r in results], "dry_run": effective_dry}

    def _set_state(self, ctx: RequestSecurityContext, *, tenant_id: str, automation_id: str, enabled: bool, paused: bool, state: str) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        self._access.require(ctx, PERM_AUTOMATION_ENABLE, tenant_id=tenant)
        current = self._store.get(tenant_id=tenant, automation_id=automation_id)
        if current is None:
            raise ControlledAutomationError(AUTOMATION_NOT_FOUND, automation_id)
        if enabled and current.risk_class in {"R3_EXTERNAL_BUSINESS_WRITE", "R4_HIGH_IMPACT"} and state == STATE_ENABLED:
            if not current.policy.requires_approval and not current.policy.allow_auto_execute:
                raise ControlledAutomationError(RISK_HITL_REQUIRED, "high_risk_enable_blocked")
        updated = replace(current, enabled=enabled, paused=paused, state=state, updated_at=self._now_iso())
        self._store.update(updated, expected_version=current.version)
        self._store.append_audit(tenant_id=tenant, automation_id=automation_id, event_type=state.lower(), payload={})
        return self._def_dict(updated)

    def _policy_from_payload(self, raw: dict[str, Any]) -> PolicyEnvelope:
        allowed = raw.get("allowed_action_types")
        return PolicyEnvelope(
            allowed_action_types=tuple(allowed) if allowed else tuple(ALLOWED_ACTIONS),
            allowed_integration_ids=tuple(raw.get("allowed_integration_ids") or ()),
            allowed_resource_scope=tuple(raw.get("allowed_resource_scope") or ()),
            max_actions_per_run=int(raw.get("max_actions_per_run") or 10),
            max_actions_per_hour=int(raw.get("max_actions_per_hour") or 100),
            max_actions_per_day=int(raw.get("max_actions_per_day") or 500),
            max_items_per_action=int(raw.get("max_items_per_action") or 50),
            requires_approval=bool(raw.get("requires_approval", False)),
            allow_auto_execute=bool(raw.get("allow_auto_execute", False)),
            dry_run=bool(raw.get("dry_run", False)),
            valid_from=raw.get("valid_from"),
            valid_until=raw.get("valid_until"),
            cooldown_seconds=int(raw.get("cooldown_seconds") or 300),
            min_delta_pct=raw.get("min_delta_pct"),
        )

    def _validate_payload(self, payload: dict[str, Any], *, partial: bool = False) -> None:
        for action in payload.get("actions") or ():
            at = str(action.get("action_type") or "")
            if at and at not in ALLOWED_ACTIONS:
                raise ControlledAutomationError(ACTION_NOT_ALLOWED, at)
            for key in action:
                if key.lower() in FORBIDDEN_PAYLOAD_KEYS:
                    raise ControlledAutomationError(ACTION_NOT_ALLOWED, key)

    @staticmethod
    def _def_dict(d: ControlledAutomationDefinition) -> dict[str, Any]:
        data = dict(d.__dict__)
        data["policy"] = dict(d.policy.__dict__)
        data["actions"] = list(d.actions)
        data["mode"] = "FIXTURE"
        data["live"] = False
        return data
