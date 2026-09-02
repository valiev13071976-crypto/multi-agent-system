"""Build scheduled automation runtime."""

from __future__ import annotations

from dataclasses import dataclass

from scheduled_automation.access import ScheduleAccessPolicy
from scheduled_automation.dispatcher import ScheduledAutomationDispatcher
from scheduled_automation.service import ScheduledAutomationService
from scheduled_automation.store import InMemoryScheduleAutomationStore


@dataclass
class ScheduledAutomationRuntime:
    service: ScheduledAutomationService
    policy: ScheduleAccessPolicy


def build_scheduled_automation_runtime(*, workflow_runtime=None) -> ScheduledAutomationRuntime:
    policy = ScheduleAccessPolicy()
    dispatcher = ScheduledAutomationDispatcher()

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

    service = ScheduledAutomationService(store=InMemoryScheduleAutomationStore(), dispatcher=dispatcher, access=policy)
    return ScheduledAutomationRuntime(service=service, policy=policy)
