"""Action dispatch through governed execution paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ActionDispatchResult:
    action_type: str
    status: str
    run_id: str | None = None
    side_effect: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ControlledAutomationDispatcher:
    dispatch_fn: Callable[..., dict[str, Any]] | None = None
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)

    def dispatch_actions(
        self,
        *,
        tenant_id: str,
        automation_id: str,
        run_id: str,
        actions: tuple[dict[str, Any], ...],
        dry_run: bool,
        execution_key: str,
    ) -> list[ActionDispatchResult]:
        results = []
        for action in actions:
            at = str(action.get("action_type") or "")
            if dry_run:
                results.append(ActionDispatchResult(action_type=at, status="DRY_RUN", details={"would_execute": True, **action}))
                self.dispatches.append({"tenant_id": tenant_id, "run_id": run_id, "action": action, "dry_run": True})
                continue
            if at.startswith("PREPARE_") or at in {"CONTENT_GENERATE", "ANALYTICS_READ", "STOCK_READ", "PRICE_READ", "SEO_ANALYZE"}:
                out = {"status": "PREPARED", "action": action}
                results.append(ActionDispatchResult(action_type=at, status="PREPARED", details=out))
                self.dispatches.append({"tenant_id": tenant_id, "run_id": run_id, "action": action, "prepared": True})
                continue
            if self.dispatch_fn:
                out = self.dispatch_fn(
                    workflow_type="controlled_automation.run",
                    version="1",
                    execution_key=execution_key,
                    tenant_id=tenant_id,
                    metadata={"automation_id": automation_id, "run_id": run_id, "action": action},
                    execution_lane="scheduled",
                )
            else:
                out = {"run_id": f"run-{run_id}-{at}", "status": "DISPATCHED"}
            record = {"tenant_id": tenant_id, "run_id": run_id, "action": action, "result": out}
            self.dispatches.append(record)
            self.side_effects.append(record)
            results.append(ActionDispatchResult(action_type=at, status="DISPATCHED", run_id=str(out.get("run_id") or ""), side_effect=True, details=out))
        return results
