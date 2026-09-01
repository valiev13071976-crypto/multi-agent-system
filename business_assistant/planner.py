"""Plan construction + dependency/cycle validation."""

from __future__ import annotations

import uuid
from decimal import Decimal

from business_assistant.errors import (
    BA_CAPABILITY_FORBIDDEN,
    BA_CAPABILITY_UNAVAILABLE,
    BA_PLAN_CYCLE,
    BA_PLAN_INVALID,
    BA_READ_ONLY_WRITE_BLOCKED,
    BusinessAssistantError,
)
from business_assistant.models import (
    MAX_PLAN_STEPS,
    STEP_WRITE,
    BusinessPlan,
    BusinessPlanStep,
    BusinessRequest,
    plan_fingerprint,
)
from business_assistant.recipes import select_recipe, steps_for_recipe


# Capability availability matrix for fixture orchestration (truthful; live=false connectors marked).
DEFAULT_CAPABILITIES: dict[str, dict] = {
    "data.ingest": {"available": True, "live": False},
    "data.normalize": {"available": True, "live": False},
    "data.compare": {"available": True, "live": False},
    "data.match": {"available": True, "live": False},
    "document.compare": {"available": True, "live": False},
    "document.extract": {"available": True, "live": False},
    "content.research": {"available": True, "live": False},
    "product_media": {"available": True, "live": False},
    "seo": {"available": True, "live": False},
    "commerce.product": {"available": True, "live": False},
    "marketplace.product": {"available": True, "live": False},
    "cms.bitrix": {"available": True, "live": False, "configured": False},
    "erp.1c": {"available": True, "live": False, "configured": False},
    "email": {"available": False, "live": False, "configured": False},
    "crm": {"available": False, "live": False, "configured": False},
    "calendar": {"available": False, "live": False, "configured": False},
    "acquisition": {"available": True, "live": False},
}


def detect_cycle(steps: list[BusinessPlanStep]) -> bool:
    graph = {s.step_id: list(s.depends_on) for s in steps}
    visiting: set[str] = set()
    done: set[str] = set()

    def dfs(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if dfs(dep):
                return True
        visiting.remove(node)
        done.add(node)
        return False

    return any(dfs(n) for n in graph)


def validate_plan(plan: BusinessPlan, *, capabilities: dict[str, dict] | None = None) -> None:
    caps = capabilities or DEFAULT_CAPABILITIES
    if not plan.steps:
        raise BusinessAssistantError(BA_PLAN_INVALID, "empty_plan")
    if len(plan.steps) > MAX_PLAN_STEPS:
        raise BusinessAssistantError(BA_PLAN_INVALID, "max_steps_exceeded")
    if detect_cycle(list(plan.steps)):
        raise BusinessAssistantError(BA_PLAN_CYCLE, "cyclic_dependencies")
    ids = {s.step_id for s in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in ids:
                raise BusinessAssistantError(BA_PLAN_INVALID, f"missing_dependency:{dep}")
        meta = caps.get(step.capability)
        if meta is None or not meta.get("available", False):
            if step.step_class == STEP_WRITE:
                raise BusinessAssistantError(BA_CAPABILITY_UNAVAILABLE, step.capability)
            # Non-write unavailable capabilities are allowed in plan; execute marks BLOCKED.
        if plan.read_only and step.step_class == STEP_WRITE:
            raise BusinessAssistantError(BA_READ_ONLY_WRITE_BLOCKED, step.step_id)
        if step.step_class == STEP_WRITE and not step.requires_approval:
            raise BusinessAssistantError(BA_PLAN_INVALID, "write_without_approval_path")


def build_plan(request: BusinessRequest, *, capabilities: dict[str, dict] | None = None) -> BusinessPlan:
    publish = request.intent == "PUBLISH" and not request.constraints.read_only and not request.constraints.show_before_publication
    recipe = select_recipe(request.intent, request.text, request.constraints)
    steps = steps_for_recipe(recipe, constraints=request.constraints, publish=publish)
    if request.read_only or request.constraints.read_only:
        steps = [s for s in steps if s.step_class != STEP_WRITE]
        steps = [
            s
            for s in steps
            if s.name
            not in {
                "apply_publication",
                "preview_and_wait_approval",
                "propose_corrections",
                "preview_send",
                "prepare_site_publication",
                "prepare_marketplace_publication",
                "site_sync_preview",
                "marketplace_selection",
            }
        ]
    approval_boundaries = tuple(s.step_id for s in steps if s.requires_approval)
    payload = {
        "recipe": recipe,
        "steps": [
            {
                "id": s.step_id,
                "name": s.name,
                "capability": s.capability,
                "class": s.step_class,
                "deps": list(s.depends_on),
                "approval": s.requires_approval,
            }
            for s in steps
        ],
        "read_only": request.read_only or request.constraints.read_only,
        "constraints": {
            "brands": list(request.constraints.brands),
            "marketplaces": list(request.constraints.marketplaces),
            "top_n": request.constraints.top_n,
            "show_before": request.constraints.show_before_publication,
        },
        "version": 1,
    }
    plan = BusinessPlan(
        plan_id=str(uuid.uuid4()),
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        version=1,
        recipe=recipe,
        steps=tuple(steps),
        fingerprint=plan_fingerprint(payload),
        read_only=request.read_only or request.constraints.read_only,
        approval_boundaries=approval_boundaries,
        estimated_cost=Decimal("0.10") * Decimal(len(steps)),
    )
    validate_plan(plan, capabilities=capabilities)
    return plan


def revise_plan(previous: BusinessPlan, *, new_steps: list[BusinessPlanStep], version: int) -> BusinessPlan:
    payload = {
        "recipe": previous.recipe,
        "steps": [{"id": s.step_id, "name": s.name, "capability": s.capability, "class": s.step_class} for s in new_steps],
        "version": version,
        "read_only": previous.read_only,
    }
    plan = BusinessPlan(
        plan_id=previous.plan_id,
        tenant_id=previous.tenant_id,
        request_id=previous.request_id,
        version=version,
        recipe=previous.recipe,
        steps=tuple(new_steps),
        fingerprint=plan_fingerprint(payload),
        read_only=previous.read_only,
        approval_boundaries=tuple(s.step_id for s in new_steps if s.requires_approval),
        estimated_cost=previous.estimated_cost,
    )
    validate_plan(plan)
    return plan
