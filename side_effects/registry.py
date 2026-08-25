from side_effects.errors import (
    SideEffectAdapterAlreadyRegisteredError,
    SideEffectAdapterNotFoundError,
)
from side_effects.models import SideEffectToolDescriptor


class SideEffectAdapterRegistry:
    """Explicit registration only. Does not import arbitrary modules."""

    def __init__(self):
        self._adapters: dict[str, object] = {}

    def register(self, adapter) -> None:
        tool_id = str(getattr(adapter, "tool_id", "") or "")
        if not tool_id:
            raise SideEffectAdapterNotFoundError("adapter_missing_tool_id")
        if tool_id in self._adapters:
            raise SideEffectAdapterAlreadyRegisteredError()
        self._adapters[tool_id] = adapter

    def get(self, tool_id: str):
        return self._adapters.get(tool_id)

    def require(self, tool_id: str):
        adapter = self.get(tool_id)
        if adapter is None:
            raise SideEffectAdapterNotFoundError()
        return adapter

    def list_descriptors(self) -> tuple[SideEffectToolDescriptor, ...]:
        rows = []
        for adapter in self._adapters.values():
            descriptor = getattr(adapter, "descriptor", None)
            if descriptor is not None:
                rows.append(descriptor)
        return tuple(rows)

    def __len__(self) -> int:
        return len(self._adapters)


def empty_adapter_registry() -> SideEffectAdapterRegistry:
    return SideEffectAdapterRegistry()
