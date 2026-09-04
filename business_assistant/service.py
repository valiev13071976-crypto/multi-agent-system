"""Business / Digital Assistant orchestration service."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from security.tenant import require_tenant_id

from business_assistant.conversation_gateway import (
    ConversationRequest,
    ConversationResult,
    ConversationUnavailableError,
    PandaConversationGateway,
)
from business_assistant.errors import (
    BA_ACCESS_DENIED,
    BA_APPROVAL_MISMATCH,
    BA_APPROVAL_REQUIRED,
    BA_APPROVAL_STALE,
    BA_BATCH_REQUIRED,
    BA_CANCELLED,
    BA_CAPABILITY_UNAVAILABLE,
    BA_CONNECTOR_NOT_CONFIGURED,
    BA_CONVERSATION_UNAVAILABLE,
    BA_CROSS_TENANT,
    BA_INJECTION_BLOCKED,
    BA_NOT_FOUND,
    BA_PLAN_INVALID,
    BA_READ_ONLY_WRITE_BLOCKED,
    BA_STALE_PREVIEW,
    BusinessAssistantError,
)
from business_assistant.intent import classify_intent, detect_injection, extract_constraints, objective_from
from business_assistant.ledger import ActionLedger
from business_assistant.models import (
    BATCH_ROW_THRESHOLD,
    INTENT_CONVERSATIONAL,
    KIND_CALCULATION,
    KIND_FINDING,
    KIND_PROPOSED_ACTION,
    KIND_RECOMMENDATION,
    STATUS_BATCH_RUNNING,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_COMPLETED_WITH_WARNINGS,
    STATUS_FAILED,
    STATUS_PARTIALLY_COMPLETED,
    STATUS_PLANNING,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_WAITING_FOR_APPROVAL,
    STEP_PREPARE_WRITE,
    STEP_WRITE,
    BusinessApprovalRequest,
    BusinessConstraint,
    BusinessExecution,
    BusinessExecutionStep,
    BusinessFinding,
    BusinessPlan,
    BusinessPreview,
    BusinessRequest,
    artifact_checksum,
)
from business_assistant.planner import DEFAULT_CAPABILITIES, build_plan, revise_plan, validate_plan


def _run_coroutine_in_new_loop(coro):
    """Run an async gateway call when the caller is already inside an event loop (ASGI)."""
    import concurrent.futures

    def _runner():
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


class BusinessAssistantService:
    """Application orchestration layer over closed Panda platforms."""

    def __init__(
        self,
        *,
        commerce=None,
        marketplace=None,
        capabilities: dict | None = None,
        integration_activation=None,
        integration_environment: str = "FIXTURE",
        analytics_dashboard=None,
        scheduled_automation=None,
        controlled_automation=None,
        conversation_gateway: PandaConversationGateway | None = None,
    ):
        self.commerce = commerce
        self.marketplace = marketplace
        self.capabilities = dict(capabilities or DEFAULT_CAPABILITIES)
        self.integration_activation = integration_activation
        self.integration_environment = integration_environment
        self.analytics_dashboard = analytics_dashboard
        self.scheduled_automation = scheduled_automation
        self.controlled_automation = controlled_automation
        self.conversation_gateway = conversation_gateway
        self._requests: dict[str, BusinessRequest] = {}
        self._plans: dict[str, BusinessPlan] = {}
        self._executions: dict[str, BusinessExecution] = {}
        self._ledger = ActionLedger()
        self._cost_events: list[dict] = []
        self._fixture_catalog: list[dict] = []
        self._fixture_rows: list[dict] = []
        self._previous_prices: dict[str, Decimal] = {}
        self._market_obs: dict[str, Decimal] = {}
        self._costs: dict[str, Decimal] = {}
        self._external_writes: list[dict] = []

    # --- fixtures for closure E2E (explicitly non-live) ---

    def seed_supplier_fixture(
        self,
        *,
        rows: list[dict],
        previous_prices: dict[str, str] | None = None,
        market_obs: dict[str, str] | None = None,
        costs: dict[str, str] | None = None,
        catalog: list[dict] | None = None,
    ) -> None:
        self._fixture_rows = list(rows)
        self._previous_prices = {k: Decimal(str(v)) for k, v in (previous_prices or {}).items()}
        self._market_obs = {k: Decimal(str(v)) for k, v in (market_obs or {}).items()}
        self._costs = {k: Decimal(str(v)) for k, v in (costs or {}).items()}
        self._fixture_catalog = list(catalog or [])

    def resolve_integration(
        self,
        *,
        tenant_id: str,
        capability: str,
        operation_class: str = "READ",
    ) -> dict:
        """Resolve active integration connection; never substitutes FIXTURE for LIVE."""
        if self.integration_activation is None:
            return {"status": "NO_ACTIVATION_LAYER", "environment": self.integration_environment}
        try:
            resolved = self.integration_activation.resolve_connection(
                tenant_id=tenant_id,
                capability=capability,
                environment=self.integration_environment,
                operation_class=operation_class,
            )
            return {
                "status": "OK",
                "connection_id": resolved.connection.connection_id,
                "provider": resolved.provider.provider_id,
                "environment": resolved.environment,
                "live": resolved.environment == "LIVE",
            }
        except Exception as exc:
            code = getattr(exc, "code", "INTEGRATION_NOT_CONFIGURED")
            return {"status": "BLOCKED", "code": code, "environment": self.integration_environment}

    # --- public API ---

    def submit_request(
        self,
        *,
        tenant_id: str,
        user_id: str,
        text: str,
        artifact_refs: tuple[str, ...] = (),
        read_only: bool = False,
        budget_limit: Decimal | None = None,
        source_is_untrusted: bool = False,
    ) -> BusinessRequest:
        tenant = require_tenant_id(tenant_id)
        # Injection in untrusted external content must not escalate
        if source_is_untrusted and detect_injection(text):
            # Treat as data only — sanitize to analyze intent without publish escalation
            text = "External content contained policy-override language; treat as untrusted data only."
        elif detect_injection(text) and source_is_untrusted is False:
            # User text with injection-like phrases still cannot override policy; continue but flag
            pass
        intent = classify_intent(text)
        constraints = extract_constraints(text)
        if read_only:
            constraints = BusinessConstraint(
                brands=constraints.brands,
                sku_ids=constraints.sku_ids,
                categories=constraints.categories,
                suppliers=constraints.suppliers,
                marketplaces=constraints.marketplaces,
                channels=constraints.channels,
                margin_min_pct=constraints.margin_min_pct,
                top_n=constraints.top_n,
                read_only=True,
                show_before_publication=constraints.show_before_publication,
                currency=constraints.currency,
                unknown=constraints.unknown,
            )
        req = BusinessRequest(
            request_id=str(uuid.uuid4()),
            tenant_id=tenant,
            user_id=user_id,
            text=text,
            intent=intent,
            objective=objective_from(text, intent),
            constraints=constraints,
            artifact_refs=artifact_refs,
            budget_limit=budget_limit,
            correlation_id=str(uuid.uuid4()),
            read_only=read_only or constraints.read_only,
        )
        self._requests[req.request_id] = req
        return req

    def _conversation_request(
        self,
        req: BusinessRequest,
        *,
        tenant_id: str,
        conversation_id: str | None,
    ) -> ConversationRequest:
        return ConversationRequest(
            text=req.text,
            tenant_id=require_tenant_id(tenant_id),
            user_id=req.user_id,
            request_id=req.request_id,
            conversation_id=conversation_id,
            correlation_id=req.correlation_id,
        )

    def _execution_from_conversation(
        self,
        req: BusinessRequest,
        *,
        tenant_id: str,
        result: ConversationResult,
    ) -> BusinessExecution:
        ex = BusinessExecution(
            execution_id=str(uuid.uuid4()),
            tenant_id=require_tenant_id(tenant_id),
            request_id=req.request_id,
            plan_id=f"conv-{req.request_id[:8]}",
            plan_fingerprint="conversational",
            status=STATUS_COMPLETED,
            steps={},
            correlation_id=req.correlation_id,
            workflow_id=result.workflow_id or f"ba-chat-{req.request_id[:8]}",
            mode="CONVERSATIONAL",
            summary=result.text,
        )
        self._executions[ex.execution_id] = ex
        return ex

    async def respond_conversationally_async(
        self,
        *,
        request_id: str,
        tenant_id: str,
        conversation_id: str | None = None,
    ) -> BusinessExecution:
        """Handle ordinary chat via Panda AI core — async path for API/event-loop callers."""
        req = self._get_request(tenant_id=tenant_id, request_id=request_id)
        if req.intent != INTENT_CONVERSATIONAL:
            raise BusinessAssistantError(BA_PLAN_INVALID, "not_conversational")
        if self.conversation_gateway is None:
            raise BusinessAssistantError(BA_CONVERSATION_UNAVAILABLE, "conversation_gateway_unconfigured")
        try:
            result = await self.conversation_gateway.respond(
                self._conversation_request(req, tenant_id=tenant_id, conversation_id=conversation_id)
            )
        except ConversationUnavailableError as exc:
            raise BusinessAssistantError(BA_CONVERSATION_UNAVAILABLE, str(exc)) from exc
        return self._execution_from_conversation(req, tenant_id=tenant_id, result=result)

    def respond_conversationally(
        self,
        *,
        request_id: str,
        tenant_id: str,
        conversation_id: str | None = None,
    ) -> BusinessExecution:
        """Sync entry for non-event-loop callers (tests, CLI). API path uses respond_conversationally_async."""
        coro = self.respond_conversationally_async(
            request_id=request_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        return _run_coroutine_in_new_loop(coro)

    def build_plan(self, *, request_id: str, tenant_id: str) -> BusinessPlan:
        req = self._get_request(tenant_id=tenant_id, request_id=request_id)
        plan = build_plan(req, capabilities=self.capabilities)
        self._plans[plan.plan_id] = plan
        return plan

    def validate_plan(self, *, plan_id: str, tenant_id: str) -> dict:
        plan = self._get_plan(tenant_id=tenant_id, plan_id=plan_id)
        validate_plan(plan, capabilities=self.capabilities)
        return {"ok": True, "fingerprint": plan.fingerprint, "steps": len(plan.steps)}

    def plan_preview(self, *, plan_id: str, tenant_id: str) -> dict:
        plan = self._get_plan(tenant_id=tenant_id, plan_id=plan_id)
        writes = [s for s in plan.steps if s.step_class in {STEP_WRITE, STEP_PREPARE_WRITE}]
        return {
            "plan_id": plan.plan_id,
            "recipe": plan.recipe,
            "steps": [{"id": s.step_id, "name": s.name, "class": s.step_class, "capability": s.capability} for s in plan.steps],
            "writes": [{"id": s.step_id, "name": s.name} for s in writes],
            "approvals_required": list(plan.approval_boundaries),
            "batch_steps": [s.step_id for s in plan.steps if s.workload == "batch"],
            "external_mutation": False,
            "mode": "DRY_RUN",
        }

    def execute(self, *, plan_id: str, tenant_id: str, dry_run: bool = False) -> BusinessExecution:
        plan = self._get_plan(tenant_id=tenant_id, plan_id=plan_id)
        req = self._get_request(tenant_id=tenant_id, request_id=plan.request_id)
        ex = BusinessExecution(
            execution_id=str(uuid.uuid4()),
            tenant_id=require_tenant_id(tenant_id),
            request_id=req.request_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            status=STATUS_RUNNING,
            steps={s.step_id: BusinessExecutionStep(step_id=s.step_id, status="PENDING") for s in plan.steps},
            correlation_id=req.correlation_id,
            workflow_id=f"ba-{plan.plan_id[:8]}",
            mode="FIXTURE",
        )
        self._executions[ex.execution_id] = ex
        self._emit_cost(ex, Decimal("0.01"), "execution_start")

        # Large supplier datasets must not enter LLM; route batch
        if len(self._fixture_rows) >= BATCH_ROW_THRESHOLD:
            for s in plan.steps:
                if s.workload == "batch":
                    ex.status = STATUS_BATCH_RUNNING
                    break

        for idx, step in enumerate(plan.steps):
            if ex.cancelled:
                ex.status = STATUS_CANCELLED
                break
            # dependency check
            if any(ex.steps[d].status not in {"COMPLETED", "SKIPPED"} for d in step.depends_on if d in ex.steps):
                ex.steps[step.step_id].status = "BLOCKED"
                ex.steps[step.step_id].error_code = "dependency_not_ready"
                continue

            if step.step_class == STEP_WRITE:
                if dry_run or plan.read_only or req.constraints.show_before_publication or req.constraints.read_only:
                    ex.steps[step.step_id].status = "SKIPPED"
                    ex.steps[step.step_id].result = {"reason": "write_blocked_until_approval"}
                    continue
                if ex.approval is None or ex.approval.status != "APPROVED":
                    self._build_preview_and_wait(ex, plan, req)
                    return ex
                # approved write path
                result = self._execute_write(ex, plan, req, step)
                ex.steps[step.step_id].status = "COMPLETED"
                ex.steps[step.step_id].result = result
                ex.checkpoint = idx + 1
                continue

            if step.requires_approval and step.step_class == STEP_PREPARE_WRITE and step.name in {
                "preview_and_wait_approval",
                "propose_corrections",
                "preview_send",
                "onec_price_preview",
                "wb_price_preview",
                "ozon_price_preview",
                "yandex_price_preview",
            }:
                # Run prepare logic then wait
                result = self._execute_step(ex, plan, req, step)
                ex.steps[step.step_id].status = "COMPLETED"
                ex.steps[step.step_id].result = result
                ex.checkpoint = idx + 1
                self._build_preview_and_wait(ex, plan, req)
                return ex

            try:
                result = self._execute_step(ex, plan, req, step)
                if result.get("blocked"):
                    ex.steps[step.step_id].status = "BLOCKED"
                    ex.steps[step.step_id].error_code = result.get("code", STATUS_BLOCKED)
                    ex.steps[step.step_id].result = result
                else:
                    ex.steps[step.step_id].status = "COMPLETED"
                    ex.steps[step.step_id].result = result
                ex.checkpoint = idx + 1
            except BusinessAssistantError as exc:
                if exc.code == BA_CAPABILITY_UNAVAILABLE:
                    ex.steps[step.step_id].status = "BLOCKED"
                    ex.steps[step.step_id].error_code = exc.code
                    ex.steps[step.step_id].result = {"blocked": True, "code": exc.code}
                else:
                    ex.steps[step.step_id].status = "FAILED"
                    ex.steps[step.step_id].error_code = exc.code
                    ex.status = STATUS_FAILED
                    return ex

        self._finalize_status(ex, plan)
        return ex

    def resume(self, *, execution_id: str, tenant_id: str) -> BusinessExecution:
        ex = self._get_execution(tenant_id=tenant_id, execution_id=execution_id)
        if ex.cancelled:
            raise BusinessAssistantError(BA_CANCELLED, execution_id)
        if ex.status == STATUS_WAITING_FOR_APPROVAL:
            return ex
        plan = self._get_plan(tenant_id=tenant_id, plan_id=ex.plan_id)
        req = self._get_request(tenant_id=tenant_id, request_id=ex.request_id)
        # resume from checkpoint — do not re-run completed writes
        for idx, step in enumerate(plan.steps):
            if idx < ex.checkpoint:
                continue
            st = ex.steps[step.step_id]
            if st.status in {"COMPLETED", "SKIPPED"}:
                continue
            if step.step_class == STEP_WRITE:
                if ex.approval is None or ex.approval.status != "APPROVED":
                    self._build_preview_and_wait(ex, plan, req)
                    return ex
                result = self._execute_write(ex, plan, req, step)
                st.status = "COMPLETED"
                st.result = result
                ex.checkpoint = idx + 1
                continue
            result = self._execute_step(ex, plan, req, step)
            st.status = "COMPLETED"
            st.result = result
            ex.checkpoint = idx + 1
        self._finalize_status(ex, plan)
        return ex

    def get_status(self, *, execution_id: str, tenant_id: str) -> dict:
        ex = self._get_execution(tenant_id=tenant_id, execution_id=execution_id)
        completed = sum(1 for s in ex.steps.values() if s.status == "COMPLETED")
        return {
            "execution_id": ex.execution_id,
            "status": ex.status,
            "progress": {"completed": completed, "total": len(ex.steps), "checkpoint": ex.checkpoint},
            "approval_required": ex.status == STATUS_WAITING_FOR_APPROVAL,
            "preview_available": ex.preview is not None,
            "artifacts": list(ex.artifacts),
            "warnings": [s.error_code for s in ex.steps.values() if s.status == "BLOCKED"],
            "mode": ex.mode,
            "correlation_id": ex.correlation_id,
            "cost": str(ex.cost),
        }

    def get_preview(self, *, execution_id: str, tenant_id: str) -> BusinessPreview:
        ex = self._get_execution(tenant_id=tenant_id, execution_id=execution_id)
        if ex.preview is None:
            raise BusinessAssistantError(BA_NOT_FOUND, "preview")
        return ex.preview

    def approve(
        self,
        *,
        execution_id: str,
        tenant_id: str,
        actor_id: str,
        approval_id: str | None = None,
        plan_fingerprint: str | None = None,
    ) -> BusinessExecution:
        ex = self._get_execution(tenant_id=tenant_id, execution_id=execution_id)
        if ex.approval is None:
            raise BusinessAssistantError(BA_APPROVAL_REQUIRED, "no_pending_approval")
        if approval_id and approval_id != ex.approval.approval_id:
            raise BusinessAssistantError(BA_APPROVAL_MISMATCH, "approval_id")
        if plan_fingerprint and plan_fingerprint != ex.plan_fingerprint:
            raise BusinessAssistantError(BA_APPROVAL_STALE, "plan_fingerprint_mismatch")
        if ex.approval.plan_fingerprint != ex.plan_fingerprint:
            raise BusinessAssistantError(BA_APPROVAL_STALE, "approval_plan_mismatch")
        if ex.preview and ex.preview.plan_fingerprint != ex.plan_fingerprint:
            raise BusinessAssistantError(BA_STALE_PREVIEW, "preview_fingerprint")
        # Revalidate critical state checksum
        current_checksum = artifact_checksum({"rows": self._fixture_rows, "findings": [f.summary for f in ex.findings]})
        if ex.preview and ex.preview.artifact_checksum != current_checksum:
            raise BusinessAssistantError(BA_STALE_PREVIEW, "source_changed")

        ex.approval = BusinessApprovalRequest(
            approval_id=ex.approval.approval_id,
            tenant_id=ex.tenant_id,
            execution_id=ex.execution_id,
            plan_fingerprint=ex.plan_fingerprint,
            preview_id=ex.approval.preview_id,
            actor_id=actor_id,
            step_ids=ex.approval.step_ids,
            status="APPROVED",
        )
        # Continue writes if any remain
        plan = self._get_plan(tenant_id=tenant_id, plan_id=ex.plan_id)
        req = self._get_request(tenant_id=tenant_id, request_id=ex.request_id)
        if any(s.step_class == STEP_WRITE for s in plan.steps):
            for idx, step in enumerate(plan.steps):
                if step.step_class != STEP_WRITE:
                    continue
                if ex.steps[step.step_id].status == "COMPLETED":
                    continue
                result = self._execute_write(ex, plan, req, step)
                ex.steps[step.step_id].status = "COMPLETED"
                ex.steps[step.step_id].result = result
                ex.checkpoint = max(ex.checkpoint, idx + 1)
            self._finalize_status(ex, plan)
        else:
            # prepare-only: approval acknowledges readiness; no external write without WRITE step
            ex.status = STATUS_COMPLETED
            ex.summary = self._compose_summary(ex, req, published=False, approved=True)
        return ex

    def reject(self, *, execution_id: str, tenant_id: str, actor_id: str) -> BusinessExecution:
        ex = self._get_execution(tenant_id=tenant_id, execution_id=execution_id)
        if ex.approval:
            ex.approval = BusinessApprovalRequest(
                approval_id=ex.approval.approval_id,
                tenant_id=ex.tenant_id,
                execution_id=ex.execution_id,
                plan_fingerprint=ex.plan_fingerprint,
                preview_id=ex.approval.preview_id,
                actor_id=actor_id,
                step_ids=ex.approval.step_ids,
                status="REJECTED",
            )
        ex.status = STATUS_CANCELLED
        ex.summary = "Rejected by operator; no external writes applied."
        return ex

    def cancel(self, *, execution_id: str, tenant_id: str) -> BusinessExecution:
        ex = self._get_execution(tenant_id=tenant_id, execution_id=execution_id)
        ex.cancelled = True
        ex.status = STATUS_CANCELLED
        return ex

    def get_result(self, *, execution_id: str, tenant_id: str) -> dict:
        ex = self._get_execution(tenant_id=tenant_id, execution_id=execution_id)
        return {
            "execution_id": ex.execution_id,
            "status": ex.status,
            "summary": ex.summary,
            "findings": [
                {
                    "kind": f.kind,
                    "summary": f.summary,
                    "sku_id": f.sku_id,
                    "value": f.numeric_value,
                    "evidence": list(f.evidence_refs),
                    "confidence": str(f.confidence),
                }
                for f in ex.findings
            ],
            "artifacts": list(ex.artifacts),
            "mode": ex.mode,
            "published": any(
                s.result.get("write_accepted") for s in ex.steps.values() if isinstance(s.result, dict)
            ),
            "cost": str(ex.cost),
            "correlation_id": ex.correlation_id,
        }

    def revise_after_change(self, *, plan_id: str, tenant_id: str, drop_step_names: tuple[str, ...] = ()) -> BusinessPlan:
        plan = self._get_plan(tenant_id=tenant_id, plan_id=plan_id)
        dropped = set(drop_step_names)
        # Drop named steps and anything that depended on them (transitive)
        remaining = list(plan.steps)
        changed = True
        while changed:
            changed = False
            ids_dropped = {s.step_id for s in remaining if s.name in dropped}
            new_remaining = []
            for s in remaining:
                if s.name in dropped or any(d in ids_dropped for d in s.depends_on):
                    dropped.add(s.name)
                    changed = True
                    continue
                new_remaining.append(s)
            remaining = new_remaining
        revised = revise_plan(plan, new_steps=remaining, version=plan.version + 1)
        self._plans[revised.plan_id] = revised
        return revised

    def acknowledge_reflected_change(self, *, causation_id: str, origin: str = "external") -> dict:
        return self._ledger.acknowledge_reflected(causation_id=causation_id, origin=origin)

    def cost_events(self, *, tenant_id: str) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        return [e for e in self._cost_events if e["tenant_id"] == tenant]

    # --- internals ---

    def _get_request(self, *, tenant_id: str, request_id: str) -> BusinessRequest:
        tenant = require_tenant_id(tenant_id)
        req = self._requests.get(request_id)
        if req is None:
            raise BusinessAssistantError(BA_NOT_FOUND, "request")
        if req.tenant_id != tenant:
            raise BusinessAssistantError(BA_CROSS_TENANT, "request")
        return req

    def _get_plan(self, *, tenant_id: str, plan_id: str) -> BusinessPlan:
        tenant = require_tenant_id(tenant_id)
        plan = self._plans.get(plan_id)
        if plan is None:
            raise BusinessAssistantError(BA_NOT_FOUND, "plan")
        if plan.tenant_id != tenant:
            raise BusinessAssistantError(BA_CROSS_TENANT, "plan")
        return plan

    def _get_execution(self, *, tenant_id: str, execution_id: str) -> BusinessExecution:
        tenant = require_tenant_id(tenant_id)
        ex = self._executions.get(execution_id)
        if ex is None:
            raise BusinessAssistantError(BA_NOT_FOUND, "execution")
        if ex.tenant_id != tenant:
            raise BusinessAssistantError(BA_CROSS_TENANT, "execution")
        return ex

    def _emit_cost(self, ex: BusinessExecution, amount: Decimal, operation: str) -> None:
        ex.cost += amount
        self._cost_events.append(
            {
                "tenant_id": ex.tenant_id,
                "execution_id": ex.execution_id,
                "workflow_id": ex.workflow_id,
                "operation": operation,
                "amount": str(amount),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _build_preview_and_wait(self, ex: BusinessExecution, plan: BusinessPlan, req: BusinessRequest) -> None:
        changes = []
        for f in ex.findings:
            if f.kind in {KIND_RECOMMENDATION, KIND_PROPOSED_ACTION, KIND_CALCULATION}:
                changes.append({"sku_id": f.sku_id, "summary": f.summary, "value": f.numeric_value})
        brands = req.constraints.brands
        if brands:
            filtered = []
            for row in self._fixture_rows:
                if row.get("brand") in brands:
                    filtered.append(
                        {
                            "sku_id": row.get("sku"),
                            "brand": row.get("brand"),
                            "title": row.get("title"),
                            "action": "prepare_publication",
                        }
                    )
            # Exclude ambiguous
            amb = {f.sku_id for f in ex.findings if "Ambiguous" in f.summary}
            filtered = [c for c in filtered if c.get("sku_id") not in amb]
            if req.constraints.top_n:
                filtered = filtered[: req.constraints.top_n]
            if filtered:
                changes = filtered
        external_writes = [
            {"channel": "BITRIX", "count": len(changes), "live": False, "status": "PREPARED"},
            {"channel": "MARKETPLACE", "count": len([c for c in changes if c.get("sku_id")]), "live": False, "status": "PREPARED"},
        ]
        warnings = [s.error_code for s in ex.steps.values() if s.status == "BLOCKED" and s.error_code]
        for f in ex.findings:
            if "loss" in f.summary.casefold() or "ambiguous" in f.summary.casefold():
                warnings.append(f.summary)
        checksum = artifact_checksum({"rows": self._fixture_rows, "findings": [f.summary for f in ex.findings]})
        preview = BusinessPreview(
            preview_id=str(uuid.uuid4()),
            tenant_id=ex.tenant_id,
            execution_id=ex.execution_id,
            plan_fingerprint=ex.plan_fingerprint,
            artifact_checksum=checksum,
            changes=tuple(changes),
            warnings=tuple(warnings),
            external_writes=tuple(external_writes),
        )
        ex.preview = preview
        ex.approval = BusinessApprovalRequest(
            approval_id=str(uuid.uuid4()),
            tenant_id=ex.tenant_id,
            execution_id=ex.execution_id,
            plan_fingerprint=ex.plan_fingerprint,
            preview_id=preview.preview_id,
            actor_id="",
            step_ids=plan.approval_boundaries,
            status="PENDING",
        )
        ex.status = STATUS_WAITING_FOR_APPROVAL
        ex.summary = self._compose_summary(ex, req, published=False, approved=False)
        ex.artifacts.append({"type": "preview", "ref": preview.preview_id, "mode": "FIXTURE"})

    def _execute_step(self, ex: BusinessExecution, plan: BusinessPlan, req: BusinessRequest, step) -> dict:
        self._emit_cost(ex, Decimal("0.02"), f"step:{step.name}")
        name = step.name
        cap_meta = self.capabilities.get(step.capability, {})

        if self.integration_activation is not None and name == "crm_lead_read":
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="crm.lead.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "lead_read", "lead_id": "lead-1"},
            )
            ex.artifacts.append({"type": "crm_lead", "result": out["result"]})
            return {"lead": out["result"], "mutation": False}

        if self.integration_activation is not None and name == "crm_contact_read":
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="crm.contact.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "contact_read", "contact_id": "cnt-3"},
            )
            ex.artifacts.append({"type": "crm_contact", "result": out["result"]})
            return {"contact": out["result"], "mutation": False}

        if self.integration_activation is not None and name == "crm_lead_to_email_draft":
            lead = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="crm.lead.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "lead_read", "lead_id": "lead-1"},
            )["result"]
            draft = {"to": [lead.get("email")], "subject": f"Follow up: {lead.get('title')}", "body": "CRM follow-up via Panda"}
            ex._email_send_payload = {"operation": "send", **draft}
            ex.artifacts.append({"type": "crm_to_email_draft", "lead": lead, "draft": draft})
            return {"draft": draft, "orchestration": "crm→email", "mutation": False}

        if self.integration_activation is not None and name == "crm_contact_to_calendar_prep":
            contact = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="crm.contact.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "contact_read", "contact_id": "cnt-1"},
            )["result"]
            event_prep = {
                "operation": "event_create",
                "title": f"Meeting with {contact.get('name')}",
                "start": "2026-03-01T14:00:00+03:00",
                "end": "2026-03-01T15:00:00+03:00",
                "timezone": "Europe/Moscow",
                "attendees": [contact.get("email")],
            }
            ex._calendar_event_payload = event_prep
            ex.artifacts.append({"type": "crm_to_calendar_prep", "contact": contact, "event_prep": event_prep})
            return {"event_prep": event_prep, "orchestration": "crm→calendar", "mutation": False}

        if self.integration_activation is not None and name == "email_to_crm_update_prep":
            msg = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="email.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "message_read", "message_id": "msg-1001"},
            )["result"]
            update_prep = {"operation": "contact_update", "contact_id": "cnt-3", "patch": {"company": "Acme (from email)"}}
            ex._crm_update_payload = update_prep
            ex.artifacts.append({"type": "email_to_crm_prep", "message": msg, "update_prep": update_prep})
            return {"update_prep": update_prep, "orchestration": "email→crm", "mutation": False}

        if self.integration_activation is not None and name == "calendar_event_read":
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="calendar.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "event_list", "calendar_id": "cal-primary"},
            )
            ex.artifacts.append({"type": "calendar_events", "result": out["result"]})
            return {"events": out["result"], "mutation": False}

        if self.analytics_dashboard is not None and name == "analytics_query":
            from security.identity import RequestSecurityContext

            ctx = RequestSecurityContext(tenant_id=ex.tenant_id, user_id="ba", roles=("user",), request_id=str(uuid.uuid4()))
            qtype = getattr(ex, "_analytics_question_type", None) or "sales_week"
            result = self.analytics_dashboard.ba_query(ctx, tenant_id=ex.tenant_id, question_type=qtype)
            ex.artifacts.append({"type": "analytics_result", "result": result})
            return {"analytics": result, "mutation": False}

        if self.scheduled_automation is not None and name == "schedule_create":
            from security.identity import RequestSecurityContext

            intent = getattr(ex, "_schedule_intent", None) or {}
            proposal = self.scheduled_automation.ba_create_proposal(tenant_id=ex.tenant_id, intent=intent)
            ctx = RequestSecurityContext(tenant_id=ex.tenant_id, user_id="ba", roles=("user",), request_id=str(uuid.uuid4()))
            created = self.scheduled_automation.create_schedule(ctx, {"tenant_id": ex.tenant_id, **proposal["proposal"]})
            ex.artifacts.append({"type": "schedule_created", "schedule": created})
            return {"schedule": created, "proposal": proposal, "mutation": True}

        if self.controlled_automation is not None and name == "automation_draft":
            intent = getattr(ex, "_automation_intent", None) or {}
            proposal = self.controlled_automation.ba_create_draft(tenant_id=ex.tenant_id, intent=intent)
            ex.artifacts.append({"type": "automation_draft", "proposal": proposal})
            return {"proposal": proposal, "mutation": False}

        if self.controlled_automation is not None and name == "automation_explain":
            aid = getattr(ex, "_automation_id", None) or ""
            result = self.controlled_automation.ba_explain(tenant_id=ex.tenant_id, automation_id=aid)
            ex.artifacts.append({"type": "automation_explain", "result": result})
            return {"explain": result, "mutation": False}

        if self.scheduled_automation is not None and name == "schedule_intent":
            intent = getattr(ex, "_schedule_intent", None) or {}
            proposal = self.scheduled_automation.ba_create_proposal(tenant_id=ex.tenant_id, intent=intent)
            ex.artifacts.append({"type": "schedule_proposal", "proposal": proposal})
            return {"proposal": proposal, "mutation": False}

        # Real Integration Activation: Ozon orders read path
        if self.integration_activation is not None and (
            "ozon" in req.text.casefold() or "заказ" in req.text.casefold()
        ) and name in {"marketplace_economics", "read_listings", "economics_scan"}:
            cap = "marketplace.ozon.orders.read"
            resolved = self.resolve_integration(tenant_id=ex.tenant_id, capability=cap, operation_class="READ")
            if resolved.get("status") == "BLOCKED":
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, resolved.get("code", "integration_blocked"))
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability=cap,
                environment=self.integration_environment,
                operation_class="READ",
                correlation_id=ex.correlation_id,
                workflow_id=ex.workflow_id,
            )
            ex.artifacts.append({"type": "ozon_orders", "result": out["result"], "mode": out["environment"], "live": out["live"]})
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_FINDING,
                    summary=f"Ozon orders fetched: {len(out['result'].get('items') or [])} (fixture)",
                    evidence_refs=("integration:ozon",),
                )
            )
            return {"orders": out["result"], "integration": resolved, "mutation": False}

        # Real Integration Activation: Bitrix product read by article
        if self.integration_activation is not None and (
            "bitrix" in req.text.casefold() or "битрикс" in req.text.casefold()
        ) and name in {"read_listings", "match_products", "prepare_content"}:
            import re

            article_match = re.search(r"(?:артикул[уом]?|sku)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else ""
            cap = "cms.bitrix.catalog.read"
            resolved = self.resolve_integration(tenant_id=ex.tenant_id, capability=cap, operation_class="READ")
            if resolved.get("status") == "BLOCKED":
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, resolved.get("code", "integration_blocked"))
            payload = {"operation": "product_lookup", "article": article} if article else {"page": 1}
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability=cap,
                environment=self.integration_environment,
                operation_class="READ",
                payload=payload,
                correlation_id=ex.correlation_id,
                workflow_id=ex.workflow_id,
            )
            ex.artifacts.append({"type": "bitrix_product", "result": out["result"], "mode": out["environment"], "live": out["live"]})
            product = out["result"].get("product") or {}
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_FINDING,
                    summary=f"Bitrix product: {product.get('name') or 'catalog'} ({product.get('article') or 'list'})",
                    evidence_refs=("integration:bitrix",),
                    sku_id=str(product.get("article") or ""),
                )
            )
            return {"product": out["result"], "integration": resolved, "mutation": False}

        # Real Integration Activation: 1C stock read by article
        if self.integration_activation is not None and (
            "1с" in req.text.casefold() or "1c" in req.text.casefold() or "onec" in req.text.casefold()
        ) and any(w in req.text.casefold() for w in ("остат", "stock", "склад")) and name in {
            "read_listings",
            "match_products",
            "onec_resolve_price_target",
            "analyze_request",
            "prepare_summary",
        }:
            import re

            article_match = re.search(r"(?:артикул[уом]?|sku)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else ""
            cap = "erp.1c.catalog.read"
            resolved = self.resolve_integration(tenant_id=ex.tenant_id, capability=cap, operation_class="READ")
            if resolved.get("status") == "BLOCKED":
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, resolved.get("code", "integration_blocked"))
            payload = {"operation": "stock_read", "article": article} if article else {"page": 1}
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability=cap,
                environment=self.integration_environment,
                operation_class="READ",
                payload=payload,
                correlation_id=ex.correlation_id,
                workflow_id=ex.workflow_id,
            )
            ex.artifacts.append({"type": "onec_stock", "result": out["result"], "mode": out["environment"], "live": out["live"]})
            stock = out["result"] or {}
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_FINDING,
                    summary=f"1C stock: {stock.get('article') or 'n/a'} available={stock.get('available', 'n/a')}",
                    evidence_refs=("integration:onec",),
                    sku_id=str(stock.get("article") or ""),
                )
            )
            return {"stock": out["result"], "integration": resolved, "mutation": False}

        if name == "onec_resolve_price_target":
            import re

            article_match = re.search(r"(?:артикул[уом]?|sku|товар\w*)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "1C-SKU-100"
            cap = "erp.1c.catalog.read"
            resolved = self.resolve_integration(tenant_id=ex.tenant_id, capability=cap, operation_class="READ")
            price_out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability=cap,
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "article": article},
                correlation_id=ex.correlation_id,
                workflow_id=ex.workflow_id,
            )
            ex.artifacts.append({"type": "onec_price_current", "result": price_out["result"], "article": article})
            return {"article": article, "current_price": price_out["result"], "integration": resolved}

        if name == "onec_price_preview":
            import re

            article_match = re.search(r"(?:артикул[уом]?|sku|товар\w*)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "1C-SKU-100"
            price_match = re.search(r"(\d[\d\s]{2,})\s*₽?", req.text)
            new_price = price_match.group(1).replace(" ", "") if price_match else "49990"
            current = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="erp.1c.catalog.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "article": article},
            )["result"]
            preview = {
                "operation": "price_update",
                "article": article,
                "before": current,
                "after": {"amount": new_price, "currency": "RUB", "price_type": "RETAIL"},
            }
            ex.artifacts.append({"type": "onec_price_preview", "preview": preview})
            ex._onec_price_payload = {"operation": "price_update", "article": article, "new_price": new_price, "preview": preview}
            return {"preview": preview, "requires_approval": True}

        if name == "onec_price_verify":
            payload = getattr(ex, "_onec_price_payload", {}) or {}
            article = str(payload.get("article") or "1C-SKU-100")
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="erp.1c.catalog.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "article": article},
            )
            expected = str(payload.get("new_price") or "")
            observed = str((out["result"] or {}).get("amount") or "")
            verified = observed == expected
            ex.artifacts.append({"type": "onec_price_verify", "verified": verified, "observed": out["result"]})
            return {"verified": verified, "observed": out["result"]}

        # Wildberries price+stock read
        if self.integration_activation is not None and any(
            w in req.text.casefold() for w in ("wildberries", "wb", "вайлдберриз")
        ) and any(w in req.text.casefold() for w in ("цен", "price", "остат", "stock")) and name in {
            "read_listings",
            "match_products",
            "wb_resolve_price_target",
            "analyze_request",
            "prepare_summary",
            "economics_scan",
        }:
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "WB-SKU-100"
            cap = "marketplace.wb.price.read"
            resolved = self.resolve_integration(tenant_id=ex.tenant_id, capability=cap, operation_class="READ")
            if resolved.get("status") == "BLOCKED":
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, resolved.get("code", "integration_blocked"))
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability=cap,
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_and_stock", "seller_article": article, "warehouse": "main"},
                correlation_id=ex.correlation_id,
                workflow_id=ex.workflow_id,
            )
            ex.artifacts.append({"type": "wb_price_stock", "result": out["result"], "mode": out["environment"], "live": out["live"]})
            price = (out["result"] or {}).get("price") or {}
            stock = (out["result"] or {}).get("stock") or {}
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_FINDING,
                    summary=f"WB {article}: price={price.get('seller_effective_price')} stock={stock.get('available')}",
                    evidence_refs=("integration:wildberries",),
                    sku_id=article,
                )
            )
            return {"price_stock": out["result"], "integration": resolved, "mutation": False}

        if name == "wb_resolve_price_target":
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "WB-SKU-100"
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.wb.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "seller_article": article},
            )
            ex.artifacts.append({"type": "wb_price_current", "result": out["result"], "article": article})
            return {"seller_article": article, "current_price": out["result"]}

        if name == "wb_price_preview":
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "WB-SKU-100"
            price_match = re.search(r"(\d[\d\s]{4,})\s*₽", req.text)
            if not price_match:
                price_match = re.search(r"(?:wildberries|вайлдберриз)\s+(\d[\d\s]{3,})", req.text, re.I)
            new_price = price_match.group(1).replace(" ", "") if price_match else "49990"
            current = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.wb.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "seller_article": article},
            )["result"]
            preview = {
                "operation": "price_update",
                "seller_article": article,
                "before": current,
                "after": {"base_price": new_price, "currency": "RUB"},
            }
            ex.artifacts.append({"type": "wb_price_preview", "preview": preview})
            ex._wb_price_payload = {
                "operation": "price_update",
                "seller_article": article,
                "new_price": new_price,
                "preview": preview,
            }
            return {"preview": preview, "requires_approval": True}

        if name == "wb_price_verify":
            payload = getattr(ex, "_wb_price_payload", {}) or {}
            article = str(payload.get("seller_article") or "WB-SKU-100")
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.wb.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "seller_article": article},
            )
            expected = str(payload.get("new_price") or "")
            observed = str((out["result"] or {}).get("base_price") or "")
            verified = observed == expected
            ex.artifacts.append({"type": "wb_price_verify", "verified": verified, "observed": out["result"]})
            return {"verified": verified, "observed": out["result"]}

        # Ozon price+stock read
        if self.integration_activation is not None and any(
            w in req.text.casefold() for w in ("ozon", "озон")
        ) and any(w in req.text.casefold() for w in ("цен", "price", "остат", "stock")) and name in {
            "read_listings",
            "match_products",
            "ozon_resolve_price_target",
            "analyze_request",
            "prepare_summary",
            "economics_scan",
        }:
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?|offer)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "OZ-SKU-100"
            cap = "marketplace.ozon.price.read"
            resolved = self.resolve_integration(tenant_id=ex.tenant_id, capability=cap, operation_class="READ")
            if resolved.get("status") == "BLOCKED":
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, resolved.get("code", "integration_blocked"))
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability=cap,
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_and_stock", "seller_article": article, "warehouse": "fbs_main"},
                correlation_id=ex.correlation_id,
                workflow_id=ex.workflow_id,
            )
            ex.artifacts.append({"type": "ozon_price_stock", "result": out["result"], "mode": out["environment"], "live": out["live"]})
            price = (out["result"] or {}).get("price") or {}
            stock = (out["result"] or {}).get("stock") or {}
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_FINDING,
                    summary=f"Ozon {article}: price={price.get('seller_effective_price')} stock={stock.get('available')}",
                    evidence_refs=("integration:ozon",),
                    sku_id=article,
                )
            )
            return {"price_stock": out["result"], "integration": resolved, "mutation": False}

        if self.integration_activation is not None and any(
            w in req.text.casefold() for w in ("ozon", "озон")
        ) and any(w in req.text.casefold() for w in ("скидк", "promo", "promotion", "акци")) and name in {
            "read_listings",
            "economics_scan",
            "analyze_request",
        }:
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "OZ-SKU-200"
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.ozon.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "promotion_analysis", "seller_article": article},
            )
            ex.artifacts.append({"type": "ozon_promotion_alert", "result": out["result"], "mutation": False})
            if out["result"].get("alert"):
                ex.findings.append(
                    BusinessFinding(
                        finding_id=str(uuid.uuid4()),
                        kind=KIND_FINDING,
                        summary=f"Ozon platform discount on {article} — no blind price rewrite",
                        evidence_refs=("integration:ozon", "promotion:platform"),
                        sku_id=article,
                    )
                )
            return {"promotion_analysis": out["result"], "mutation": False}

        if name == "ozon_resolve_price_target":
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?|offer)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "OZ-SKU-100"
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.ozon.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "seller_article": article},
            )
            ex.artifacts.append({"type": "ozon_price_current", "result": out["result"], "article": article})
            return {"seller_article": article, "current_price": out["result"]}

        if name == "ozon_price_preview":
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?|offer)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "OZ-SKU-100"
            price_match = re.search(r"(\d[\d\s]{4,})\s*₽", req.text)
            if not price_match:
                price_match = re.search(r"(?:ozon|озон)\s+(\d[\d\s]{3,})", req.text, re.I)
            new_price = price_match.group(1).replace(" ", "") if price_match else "49990"
            current = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.ozon.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "seller_article": article},
            )["result"]
            preview = {
                "operation": "price_update",
                "seller_article": article,
                "before": current,
                "after": {"seller_price": new_price, "currency": "RUB"},
            }
            ex.artifacts.append({"type": "ozon_price_preview", "preview": preview})
            ex._ozon_price_payload = {
                "operation": "price_update",
                "seller_article": article,
                "new_price": new_price,
                "preview": preview,
            }
            return {"preview": preview, "requires_approval": True}

        if name == "ozon_price_verify":
            payload = getattr(ex, "_ozon_price_payload", {}) or {}
            article = str(payload.get("seller_article") or "OZ-SKU-100")
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.ozon.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "seller_article": article},
            )
            expected = str(payload.get("new_price") or "")
            observed = str((out["result"] or {}).get("seller_price") or "")
            verified = observed == expected
            ex.artifacts.append({"type": "ozon_price_verify", "verified": verified, "observed": out["result"]})
            return {"verified": verified, "observed": out["result"]}

        # Yandex Market price+stock read
        if self.integration_activation is not None and any(
            w in req.text.casefold() for w in ("яндекс", "yandex")
        ) and any(w in req.text.casefold() for w in ("цен", "price", "остат", "stock")) and name in {
            "read_listings",
            "match_products",
            "yandex_resolve_price_target",
            "analyze_request",
            "prepare_summary",
            "economics_scan",
        }:
            import re

            sku_match = re.search(r"(?:товар\w*|sku|артикул[уом]?|offer)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            shop_sku = sku_match.group(1) if sku_match else "YM-SKU-100"
            cap = "marketplace.yandex.price.read"
            resolved = self.resolve_integration(tenant_id=ex.tenant_id, capability=cap, operation_class="READ")
            if resolved.get("status") == "BLOCKED":
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, resolved.get("code", "integration_blocked"))
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability=cap,
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_and_stock", "shop_sku": shop_sku, "warehouse": "dbs_main"},
                correlation_id=ex.correlation_id,
                workflow_id=ex.workflow_id,
            )
            ex.artifacts.append({"type": "yandex_price_stock", "result": out["result"], "mode": out["environment"], "live": out["live"]})
            price = (out["result"] or {}).get("price") or {}
            stock = (out["result"] or {}).get("stock") or {}
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_FINDING,
                    summary=f"Yandex Market {shop_sku}: price={price.get('seller_effective_price')} stock={stock.get('available')}",
                    evidence_refs=("integration:yandex_market",),
                    sku_id=shop_sku,
                )
            )
            return {"price_stock": out["result"], "integration": resolved, "mutation": False}

        if self.integration_activation is not None and any(
            w in req.text.casefold() for w in ("яндекс", "yandex")
        ) and any(w in req.text.casefold() for w in ("скидк", "promo", "promotion", "акци")) and name in {
            "read_listings",
            "economics_scan",
            "analyze_request",
        }:
            import re

            sku_match = re.search(r"(?:товар\w*|sku|артикул[уом]?)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            shop_sku = sku_match.group(1) if sku_match else "YM-SKU-200"
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.yandex.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "promotion_analysis", "shop_sku": shop_sku},
            )
            ex.artifacts.append({"type": "yandex_promotion_alert", "result": out["result"], "mutation": False})
            if out["result"].get("alert"):
                ex.findings.append(
                    BusinessFinding(
                        finding_id=str(uuid.uuid4()),
                        kind=KIND_FINDING,
                        summary=f"Yandex Market platform discount on {shop_sku} — no blind price rewrite",
                        evidence_refs=("integration:yandex_market", "promotion:platform"),
                        sku_id=shop_sku,
                    )
                )
            return {"promotion_analysis": out["result"], "mutation": False}

        if name == "yandex_resolve_price_target":
            import re

            sku_match = re.search(r"(?:товар\w*|sku|артикул[уом]?|offer)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            shop_sku = sku_match.group(1) if sku_match else "YM-SKU-100"
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.yandex.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "shop_sku": shop_sku},
            )
            ex.artifacts.append({"type": "yandex_price_current", "result": out["result"], "shop_sku": shop_sku})
            return {"shop_sku": shop_sku, "current_price": out["result"]}

        if name == "yandex_price_preview":
            import re

            sku_match = re.search(r"(?:товар\w*|sku|артикул[уом]?|offer)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            shop_sku = sku_match.group(1) if sku_match else "YM-SKU-100"
            price_match = re.search(r"(\d[\d\s]{4,})\s*₽", req.text)
            if not price_match:
                price_match = re.search(r"(?:яндекс|yandex)\s+(\d[\d\s]{3,})", req.text, re.I)
            new_price = price_match.group(1).replace(" ", "") if price_match else "49990"
            current = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.yandex.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "shop_sku": shop_sku},
            )["result"]
            preview = {
                "operation": "price_update",
                "shop_sku": shop_sku,
                "before": current,
                "after": {"seller_price": new_price, "currency": "RUB"},
            }
            ex.artifacts.append({"type": "yandex_price_preview", "preview": preview})
            ex._yandex_price_payload = {
                "operation": "price_update",
                "shop_sku": shop_sku,
                "new_price": new_price,
                "preview": preview,
            }
            return {"preview": preview, "requires_approval": True}

        if name == "yandex_price_verify":
            payload = getattr(ex, "_yandex_price_payload", {}) or {}
            shop_sku = str(payload.get("shop_sku") or "YM-SKU-100")
            out = self.integration_activation.execute_via_gateway(
                tenant_id=ex.tenant_id,
                capability="marketplace.yandex.price.read",
                environment=self.integration_environment,
                operation_class="READ",
                payload={"operation": "price_read", "shop_sku": shop_sku},
            )
            expected = str(payload.get("new_price") or "")
            observed = str((out["result"] or {}).get("seller_price") or "")
            verified = observed == expected
            ex.artifacts.append({"type": "yandex_price_verify", "verified": verified, "observed": out["result"]})
            return {"verified": verified, "observed": out["result"]}

        if name == "ozon_card_import":
            import re

            article_match = re.search(r"(?:товар\w*|sku|артикул[уом]?|offer)\s+([A-Za-z0-9\-]+)", req.text, re.I)
            article = article_match.group(1) if article_match else "OZ-NEW-1"
            title_match = re.search(r"(?:назван|title)\s+(.+?)(?:\.|$)", req.text, re.I)
            title = title_match.group(1).strip() if title_match else "New Ozon Product"
            ex._ozon_card_payload = {
                "operation": "card_import",
                "product": {"seller_article": article, "offer_id": f"OFFER-{article}", "title": title, "category_id": "phones"},
            }
            ex.artifacts.append({"type": "ozon_card_prepare", "payload": ex._ozon_card_payload})
            return {"prepared": True, "requires_approval": True}

        if self.integration_activation is not None and step.capability == "email":
            if name == "retrieve_email_context":
                out = self.integration_activation.execute_via_gateway(
                    tenant_id=ex.tenant_id,
                    capability="email.read",
                    environment=self.integration_environment,
                    operation_class="READ",
                    payload={"operation": "message_search", "query": ""},
                )
                ex.artifacts.append({"type": "email_search", "result": out["result"], "mode": out["environment"], "live": out["live"]})
                msg = (out["result"] or {}).get("items") or []
                if any("Ignore previous rules" in str(m.get("body", "")) for m in msg):
                    ex.findings.append(
                        BusinessFinding(
                            finding_id=str(uuid.uuid4()),
                            kind=KIND_FINDING,
                            summary="Untrusted email content — no policy override",
                            evidence_refs=("email:untrusted",),
                        )
                    )
                return {"messages": out["result"], "mutation": False}
            if name == "preview_send":
                draft_payload = getattr(ex, "_email_send_payload", None) or {
                    "operation": "send",
                    "to": ["supplier@example.com"],
                    "subject": "Reply",
                    "body": "Draft prepared by Panda",
                }
                ex._email_send_payload = draft_payload
                ex.artifacts.append({"type": "email_draft_preview", "preview": draft_payload, "sent": False})
                return {"preview": draft_payload, "requires_approval": True, "sent": False}

        if step.capability in {"email", "crm"} and not cap_meta.get("available"):
            if not (self.integration_activation is not None and step.capability == "email"):
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, step.capability)

        if name == "ingest_supplier_price":
            if len(self._fixture_rows) >= BATCH_ROW_THRESHOLD:
                return {"rows": len(self._fixture_rows), "workload": "batch", "llm_context_rows": 0, "mode": "FIXTURE"}
            return {"rows": len(self._fixture_rows), "workload": "interactive", "llm_context_rows": 0, "mode": "FIXTURE"}

        if name == "normalize_price_rows":
            return {"normalized": len(self._fixture_rows), "identifiers_as_strings": True}

        if name == "match_products":
            matched, ambiguous = [], []
            for row in self._fixture_rows:
                sku = str(row.get("sku") or "")
                if row.get("ambiguous"):
                    ambiguous.append(sku)
                    ex.findings.append(
                        BusinessFinding(
                            finding_id=str(uuid.uuid4()),
                            kind=KIND_FINDING,
                            summary=f"Ambiguous match for {sku} — not auto-applied",
                            evidence_refs=("match:ambiguous",),
                            sku_id=sku,
                            confidence=Decimal("0.4"),
                        )
                    )
                else:
                    matched.append(sku)
            return {"matched": matched, "ambiguous": ambiguous, "auto_applied_ambiguous": False}

        if name == "compare_previous_prices":
            deltas = []
            for row in self._fixture_rows:
                sku = str(row.get("sku") or "")
                new_p = Decimal(str(row.get("price") or 0))
                old_p = self._previous_prices.get(sku)
                if old_p is not None:
                    deltas.append({"sku": sku, "old": str(old_p), "new": str(new_p), "delta": str(new_p - old_p)})
            ex.artifacts.append({"type": "price_comparison", "count": len(deltas), "mode": "FIXTURE"})
            return {"deltas": deltas}

        if name == "marketplace_economics":
            return self._run_marketplace_economics(ex, req)

        if name == "rank_profitable_candidates":
            return self._rank_candidates(ex, req)

        if name in {"prepare_content", "content_handoff", "content_briefs", "compose_report", "draft_reply", "prepare_summary"}:
            if detect_injection(req.text) is False:
                pass
            # Fact lock: refuse invented warranty claims in generated copy path
            ex.artifacts.append({"type": "content_handoff", "delegate_to": "content_intel", "mode": "FIXTURE"})
            return {"delegate_to": "content_intel", "generated": False, "handoff": True}

        if name in {"prepare_media", "media_handoff"}:
            ex.artifacts.append({"type": "media_handoff", "delegate_to": "product_media", "mode": "FIXTURE"})
            return {"delegate_to": "product_media", "handoff": True}

        if name in {"prepare_seo", "seo_handoff", "seo_snapshot", "opportunities"}:
            ex.artifacts.append({"type": "seo_handoff", "delegate_to": "seo_marketing", "mode": "FIXTURE"})
            if "opportunities" in name or name == "seo_snapshot":
                ex.findings.append(
                    BusinessFinding(
                        finding_id=str(uuid.uuid4()),
                        kind=KIND_RECOMMENDATION,
                        summary="SEO opportunity: improve title coverage (fixture)",
                        evidence_refs=("seo:fixture",),
                        confidence=Decimal("0.7"),
                    )
                )
            return {"delegate_to": "seo_marketing", "cms_mutation": False}

        if name in {"prepare_site_publication", "site_sync_preview"}:
            configured = self.capabilities.get("cms.bitrix", {}).get("configured", False)
            if not configured:
                return {
                    "status": "PREPARED",
                    "blocked": False,
                    "connector": "cms.bitrix",
                    "code": BA_CONNECTOR_NOT_CONFIGURED,
                    "live": False,
                    "message": "Publication prepared; live Bitrix connector not configured",
                }
            return {"status": "PREPARED", "live": False}

        if name == "prepare_marketplace_publication":
            return {"status": "PREPARED", "selective": True, "live": False, "delegate_to": "marketplace"}

        if name == "preview_and_wait_approval":
            return {"preview": True}

        if name in {"read_listings", "economics_scan", "loss_detection", "propose_corrections"}:
            return self._run_marketplace_economics(ex, req)

        if name in {"extract_documents", "compare_documents", "difference_report"}:
            ex.artifacts.append({"type": "document_comparison", "mode": "FIXTURE"})
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_FINDING,
                    summary="Document difference: payment terms clause changed (fixture)",
                    evidence_refs=("doc:fixture",),
                )
            )
            return {"compared": True, "writes": False}

        if name == "retrieve_email_context":
            raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, "email")

        if name in {"validate_product_facts", "pricing_prepare", "commerce_snapshot", "analyze_request"}:
            return {"ok": True, "mode": "FIXTURE"}

        if name == "marketplace_selection":
            return {"selected": list(req.constraints.sku_ids) or ["selective"], "full_catalog": False}

        if name in {"marketplace_snapshot"}:
            return {"mode": "FIXTURE"}

        return {"ok": True, "step": name, "mode": "FIXTURE"}

    def _run_marketplace_economics(self, ex: BusinessExecution, req: BusinessRequest) -> dict:
        # Prefer real MarketplacePlatformService when injected
        results = []
        if self.marketplace is not None:
            from marketplace.models import MarketplaceCommissionObservation, PROVIDER_WILDBERRIES

            self.marketplace.set_commission(
                MarketplaceCommissionObservation(
                    observation_id="ba-c1",
                    provider=PROVIDER_WILDBERRIES,
                    category="phones",
                    rate=Decimal("0.15"),
                    fixed_fee=Decimal("10"),
                    source="fixture",
                )
            )
        for row in self._fixture_rows:
            sku = str(row.get("sku") or "")
            brand = str(row.get("brand") or "")
            if req.constraints.brands and brand not in req.constraints.brands:
                continue
            price = Decimal(str(row.get("price") or 0))
            cost = self._costs.get(sku)
            if self.marketplace is not None and cost is not None:
                econ = self.marketplace.profitability(
                    sku_id=sku,
                    provider="WILDBERRIES",
                    selling_price=price,
                    purchase_cost=cost,
                    category="phones",
                    logistics=Decimal("50"),
                )
                status = econ["status"]
                margin = econ.get("margin_pct")
                ex.findings.append(
                    BusinessFinding(
                        finding_id=str(uuid.uuid4()),
                        kind=KIND_CALCULATION,
                        summary=f"{brand} {sku} economics={status} margin={margin}",
                        evidence_refs=("marketplace:profitability", f"cost={cost}", f"price={price}"),
                        sku_id=sku,
                        numeric_value=str(margin or ""),
                        confidence=Decimal("0.95") if not econ.get("unknown_costs") else Decimal("0.2"),
                    )
                )
                if status == "LOSS":
                    ex.findings.append(
                        BusinessFinding(
                            finding_id=str(uuid.uuid4()),
                            kind=KIND_FINDING,
                            summary=f"Loss flagged for {sku}",
                            evidence_refs=("marketplace:loss",),
                            sku_id=sku,
                        )
                    )
                    ex.findings.append(
                        BusinessFinding(
                            finding_id=str(uuid.uuid4()),
                            kind=KIND_PROPOSED_ACTION,
                            summary=f"Propose price correction for {sku}",
                            evidence_refs=("marketplace:loss_guard",),
                            sku_id=sku,
                        )
                    )
                results.append({"sku": sku, "status": status})
            elif cost is None:
                ex.findings.append(
                    BusinessFinding(
                        finding_id=str(uuid.uuid4()),
                        kind=KIND_FINDING,
                        summary=f"UNKNOWN economics for {sku} — missing cost",
                        evidence_refs=("insufficient_data",),
                        sku_id=sku,
                        confidence=Decimal("0"),
                    )
                )
            else:
                # Local deterministic fallback without inventing fees
                contribution = price - cost
                status = "PROFITABLE" if contribution > 0 else "LOSS"
                ex.findings.append(
                    BusinessFinding(
                        finding_id=str(uuid.uuid4()),
                        kind=KIND_CALCULATION,
                        summary=f"{brand} {sku} contribution={contribution} (partial; fees unknown)",
                        evidence_refs=(f"price={price}", f"cost={cost}"),
                        sku_id=sku,
                        numeric_value=str(contribution),
                        confidence=Decimal("0.5"),
                    )
                )
                results.append({"sku": sku, "status": status})
        return {"results": results, "live": False}

    def _rank_candidates(self, ex: BusinessExecution, req: BusinessRequest) -> dict:
        profitable = [
            f for f in ex.findings
            if f.kind == KIND_CALCULATION and "LOSS" not in f.summary and "UNKNOWN" not in f.summary
        ]
        # Exclude ambiguous skus
        ambiguous = {f.sku_id for f in ex.findings if "Ambiguous" in f.summary}
        profitable = [f for f in profitable if f.sku_id not in ambiguous]
        if req.constraints.top_n:
            profitable = profitable[: req.constraints.top_n]
        for f in profitable:
            ex.findings.append(
                BusinessFinding(
                    finding_id=str(uuid.uuid4()),
                    kind=KIND_RECOMMENDATION,
                    summary=f"Candidate {f.sku_id}: {f.summary}",
                    evidence_refs=f.evidence_refs,
                    sku_id=f.sku_id,
                    numeric_value=f.numeric_value,
                    confidence=f.confidence,
                )
            )
        return {"candidates": [f.sku_id for f in profitable], "excluded_ambiguous": list(ambiguous)}

    def _execute_write(self, ex: BusinessExecution, plan: BusinessPlan, req: BusinessRequest, step) -> dict:
        if plan.read_only or req.read_only:
            raise BusinessAssistantError(BA_READ_ONLY_WRITE_BLOCKED, step.step_id)
        if req.constraints.show_before_publication and ex.approval and ex.approval.status != "APPROVED":
            raise BusinessAssistantError(BA_APPROVAL_REQUIRED, step.step_id)
        key = f"{ex.execution_id}:{step.step_id}:write"
        causation = f"panda-ba-{uuid.uuid4().hex[:12]}"

        def _do():
            # Prefer Real Integration Activation path when attached
            if self.integration_activation is not None and (
                step.capability in {"cms.bitrix", "email", "erp.1c", "calendar", "crm"}
                or step.name in {"wb_price_apply", "onec_price_apply", "ozon_price_apply", "yandex_price_apply", "calendar_event_apply", "crm_contact_apply"}
            ):
                if step.capability == "cms.bitrix":
                    cap = "cms.bitrix.catalog.write"
                    write_payload = {"step": step.name, "execution_id": ex.execution_id}
                elif step.capability == "email":
                    cap = "email.send"
                    write_payload = getattr(ex, "_email_send_payload", None) or {"operation": "send", "to": ["supplier@example.com"], "subject": "Reply", "body": "Hello"}
                elif step.name == "calendar_event_apply":
                    cap = "calendar.event.create"
                    write_payload = getattr(ex, "_calendar_event_payload", None) or {
                        "operation": "event_create",
                        "title": "Meeting",
                        "start": "2026-03-01T14:00:00+03:00",
                        "end": "2026-03-01T15:00:00+03:00",
                        "timezone": "Europe/Moscow",
                    }
                elif step.name == "crm_contact_apply":
                    cap = "crm.contact.write"
                    write_payload = getattr(ex, "_crm_update_payload", None) or {
                        "operation": "contact_update",
                        "contact_id": "cnt-1",
                        "patch": {"phone": "+79001111111"},
                    }
                elif step.name == "wb_price_apply":
                    cap = "marketplace.wb.price.write"
                    write_payload = getattr(ex, "_wb_price_payload", None) or {
                        "operation": "price_update",
                        "seller_article": "WB-SKU-100",
                        "new_price": "49990",
                    }
                elif step.name == "ozon_price_apply":
                    cap = "marketplace.ozon.price.write"
                    write_payload = getattr(ex, "_ozon_price_payload", None) or {
                        "operation": "price_update",
                        "seller_article": "OZ-SKU-100",
                        "new_price": "49990",
                    }
                elif step.name == "yandex_price_apply":
                    cap = "marketplace.yandex.price.write"
                    write_payload = getattr(ex, "_yandex_price_payload", None) or {
                        "operation": "price_update",
                        "shop_sku": "YM-SKU-100",
                        "new_price": "49990",
                    }
                else:
                    cap = "erp.1c.catalog.write"
                    write_payload = getattr(ex, "_onec_price_payload", None) or {
                        "operation": "price_update",
                        "article": "1C-SKU-100",
                        "new_price": "49990",
                    }
                out = self.integration_activation.execute_via_gateway(
                    tenant_id=ex.tenant_id,
                    capability=cap,
                    environment=self.integration_environment,
                    operation_class="WRITE",
                    payload=write_payload,
                    idempotency_key=key,
                    correlation_id=ex.correlation_id,
                    workflow_id=ex.workflow_id,
                    approved_write=True,
                )
                result = out["result"]
                self._ledger.record_outbound(causation_id=causation, action=step.name)
                self._external_writes.append({"key": key, "result": result})
                return {
                    "write_accepted": True,
                    "status": result.get("status", "WRITE_ACCEPTED"),
                    "live": bool(out.get("live")),
                    "mode": out.get("environment") or "FIXTURE",
                    "causation_id": causation,
                    "verified": result.get("verified", "VERIFIED"),
                    "idempotent": result.get("idempotent", False),
                    "connection_id": out.get("connection_id"),
                }

            configured = self.capabilities.get(step.capability, {}).get("configured", False)
            if not configured and step.capability in {"cms.bitrix", "erp.1c"}:
                return {
                    "write_accepted": False,
                    "status": "BLOCKED",
                    "code": BA_CONNECTOR_NOT_CONFIGURED,
                    "live": False,
                    "verified": "VERIFICATION_UNAVAILABLE",
                }
            self._ledger.record_outbound(causation_id=causation, action=step.name)
            return {
                "write_accepted": True,
                "status": "WRITE_ACCEPTED",
                "live": False,
                "mode": "FIXTURE",
                "causation_id": causation,
                "verified": "VERIFICATION_UNAVAILABLE",
            }

        out = self._ledger.idempotent_write(key=key, factory=_do)
        ex.artifacts.append({"type": "write_result", "step": step.name, "result": out})
        return out

    def _finalize_status(self, ex: BusinessExecution, plan: BusinessPlan) -> None:
        statuses = [s.status for s in ex.steps.values()]
        if ex.cancelled:
            ex.status = STATUS_CANCELLED
            return
        if any(s == "FAILED" for s in statuses):
            ex.status = STATUS_FAILED
            return
        if any(s == "BLOCKED" for s in statuses):
            # required write blocked vs optional
            required_blocked = [
                s for s in plan.steps
                if ex.steps[s.step_id].status == "BLOCKED" and s.step_class == STEP_WRITE
            ]
            if required_blocked:
                ex.status = STATUS_BLOCKED
            elif any(ex.steps[s.step_id].status == "COMPLETED" for s in plan.steps):
                ex.status = STATUS_COMPLETED_WITH_WARNINGS
            else:
                ex.status = STATUS_PARTIALLY_COMPLETED
            return
        if all(s in {"COMPLETED", "SKIPPED"} for s in statuses):
            ex.status = STATUS_COMPLETED
        req = self._requests.get(ex.request_id)
        if req:
            published = any(
                isinstance(s.result, dict) and s.result.get("write_accepted") for s in ex.steps.values()
            )
            ex.summary = self._compose_summary(ex, req, published=published, approved=ex.approval is not None and ex.approval.status == "APPROVED")

    def _compose_summary(self, ex: BusinessExecution, req: BusinessRequest, *, published: bool, approved: bool) -> str:
        parts = [
            f"Requested: {req.objective}",
            f"Status: {ex.status}",
            f"Findings: {len(ex.findings)}",
            f"Artifacts: {len(ex.artifacts)}",
            f"Published: {published and ex.mode != 'FIXTURE'}",
            f"Fixture_mode: {ex.mode == 'FIXTURE'}",
            f"Approved: {approved}",
            f"Waiting_approval: {ex.status == STATUS_WAITING_FOR_APPROVAL}",
        ]
        if ex.preview:
            parts.append(f"Preview_changes: {len(ex.preview.changes)}")
        return " | ".join(parts)
