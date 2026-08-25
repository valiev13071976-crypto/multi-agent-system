from typing import Protocol, runtime_checkable

from side_effects.models import (
    AdapterExecutionResult,
    RollbackResult,
    SideEffectToolDescriptor,
)


@runtime_checkable
class SideEffectAdapter(Protocol):
    @property
    def descriptor(self) -> SideEffectToolDescriptor: ...

    @property
    def tool_id(self) -> str: ...

    @property
    def trust_level(self) -> str: ...

    @property
    def capabilities_required(self) -> tuple[str, ...]: ...

    @property
    def reversible(self) -> bool: ...

    async def execute(self, action, context) -> AdapterExecutionResult: ...

    async def rollback(self, result, context) -> RollbackResult: ...
