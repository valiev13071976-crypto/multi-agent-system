"""Deterministic DAG validation and ready-step resolution."""

from __future__ import annotations

from workflow.definition import WorkflowDefinition, WorkflowStep
from workflow.errors import WorkflowDefinitionError
from workflow.models import (
    STEP_COMPLETED,
    STEP_SKIPPED,
    WorkflowState,
)


def validate_definition(definition: WorkflowDefinition) -> None:
    """Raise WorkflowDefinitionError on missing deps or cycles."""

    steps = {s.step_id: s for s in definition.steps}
    if not steps:
        raise WorkflowDefinitionError("empty_definition", "WorkflowDefinition has no steps")
    for step in definition.steps:
        for dep in step.dependencies:
            if dep not in steps:
                raise WorkflowDefinitionError(
                    "unknown_dependency",
                    f"Step {step.step_id!r} depends on unknown step {dep!r}",
                    step_id=step.step_id,
                    dependency=dep,
                )
        if step.branch is not None:
            src = step.branch.condition.source_step_id
            if src not in steps and src != step.step_id:
                # source may be an earlier step; must exist in definition
                if src not in steps:
                    raise WorkflowDefinitionError(
                        "unknown_branch_source",
                        f"Branch on {step.step_id!r} references unknown source {src!r}",
                        step_id=step.step_id,
                        dependency=src,
                    )
            for target in step.branch.then_steps + step.branch.else_steps:
                if target not in steps:
                    raise WorkflowDefinitionError(
                        "unknown_branch_target",
                        f"Branch on {step.step_id!r} targets unknown step {target!r}",
                        step_id=step.step_id,
                        dependency=target,
                    )
    _assert_acyclic(steps)


def _assert_acyclic(steps: dict[str, WorkflowStep]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in steps}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in steps[node].dependencies:
            if color[dep] == GRAY:
                cycle = " -> ".join(stack[stack.index(dep) :] + [dep])
                raise WorkflowDefinitionError(
                    "cycle_detected",
                    f"Dependency cycle detected: {cycle}",
                )
            if color[dep] == WHITE:
                visit(dep, stack)
        # also treat branch targets as soft edges for cycle? No — branch targets
        # are not hard deps; they are activated later. Cycles via deps only.
        stack.pop()
        color[node] = BLACK

    for sid in steps:
        if color[sid] == WHITE:
            visit(sid, [])


def ready_step_ids(
    definition: WorkflowDefinition,
    state: WorkflowState,
    *,
    skipped: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Steps whose dependencies are satisfied and that are still pending/waiting."""

    skipped = skipped or frozenset()
    done = {
        STEP_COMPLETED,
        STEP_SKIPPED,
    }
    ready: list[str] = []
    for step in definition.steps:
        if step.step_id in skipped:
            continue
        record = state.step(step.step_id)
        if record is None:
            continue
        if record.status in done:
            continue
        if record.status == "running":
            continue
        if record.status == "failed":
            continue
        # pending / waiting
        deps_ok = True
        for dep in step.dependencies:
            dep_rec = state.step(dep)
            if dep_rec is None or dep_rec.status not in done:
                deps_ok = False
                break
        if deps_ok:
            ready.append(step.step_id)
    return tuple(ready)


def topological_order(definition: WorkflowDefinition) -> tuple[str, ...]:
    validate_definition(definition)
    remaining = {s.step_id: set(s.dependencies) for s in definition.steps}
    ordered: list[str] = []
    while remaining:
        ready = sorted(sid for sid, deps in remaining.items() if not deps)
        if not ready:
            raise WorkflowDefinitionError("cycle_detected", "Unresolved dependency cycle")
        for sid in ready:
            ordered.append(sid)
            del remaining[sid]
            for deps in remaining.values():
                deps.discard(sid)
    return tuple(ordered)
