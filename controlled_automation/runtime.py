"""Build controlled automation runtime."""

from __future__ import annotations

from dataclasses import dataclass

from controlled_automation.access import ControlledAutomationAccessPolicy
from controlled_automation.dispatcher import ControlledAutomationDispatcher
from controlled_automation.service import ControlledAutomationService
from controlled_automation.store import InMemoryControlledAutomationStore


@dataclass
class ControlledAutomationRuntime:
    service: ControlledAutomationService
    policy: ControlledAutomationAccessPolicy


def build_controlled_automation_runtime(*, workflow_runtime=None, scheduled_automation=None) -> ControlledAutomationRuntime:
    policy = ControlledAutomationAccessPolicy()
    dispatcher = ControlledAutomationDispatcher()

    def _dispatch(**kwargs):
        if workflow_runtime is None:
            return {"run_id": f"wf-{kwargs.get('execution_key')}", **kwargs}
        import asyncio

        coro = workflow_runtime.create_and_enqueue(
            kwargs["workflow_type"],
            kwargs.get("version") or "1",
            execution_key=kwargs["execution_key"],
            metadata=kwargs.get("metadata") or {},
            tenant_id=kwargs["tenant_id"],
            execution_lane=kwargs.get("execution_lane") or "scheduled",
        )
        if asyncio.iscoroutine(coro):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return {"run_id": kwargs["execution_key"], "async": True}
                return loop.run_until_complete(coro)
            except RuntimeError:
                return asyncio.run(coro)
        return coro

    if workflow_runtime is not None:
        dispatcher.dispatch_fn = _dispatch

    service = ControlledAutomationService(store=InMemoryControlledAutomationStore(), dispatcher=dispatcher, access=policy)
    return ControlledAutomationRuntime(service=service, policy=policy)
