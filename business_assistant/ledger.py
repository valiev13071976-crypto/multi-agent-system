"""Action ledger for loop prevention (origin/causation)."""

from __future__ import annotations

from dataclasses import dataclass, field

from business_assistant.errors import BA_LOOP_TERMINATED, BusinessAssistantError


@dataclass
class ActionLedger:
    _outbound: dict[str, str] = field(default_factory=dict)
    _acked: set[str] = field(default_factory=set)
    _writes: dict[str, dict] = field(default_factory=dict)  # idempotency_key -> result

    def record_outbound(self, *, causation_id: str, action: str) -> None:
        self._outbound[causation_id] = action

    def acknowledge_reflected(self, *, causation_id: str, origin: str = "external") -> dict:
        if origin == "panda" or causation_id in self._outbound or causation_id in self._acked:
            self._acked.add(causation_id)
            return {"terminated": True, "code": BA_LOOP_TERMINATED}
        return {"terminated": False}

    def assert_not_loop(self, *, causation_id: str, origin: str) -> None:
        result = self.acknowledge_reflected(causation_id=causation_id, origin=origin)
        if result.get("terminated"):
            raise BusinessAssistantError(BA_LOOP_TERMINATED, "reflected_own_change")

    def idempotent_write(self, *, key: str, factory) -> dict:
        if key in self._writes:
            return {**self._writes[key], "idempotent": True}
        out = factory()
        self._writes[key] = out
        return {**out, "idempotent": False}
