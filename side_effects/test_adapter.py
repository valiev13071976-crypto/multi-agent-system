import asyncio
import hashlib
import re
import uuid

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE

from side_effects.errors import RollbackExecutionError, SideEffectExecutionError
from side_effects.models import (
    TEST_OPERATION_SET_VALUE,
    TEST_RESOURCE_PREFIX,
    TEST_TOOL_ID,
    AdapterExecutionResult,
    RollbackResult,
    SideEffectToolDescriptor,
    value_fingerprint,
)


_FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"\beval\s*\(", re.I),
    re.compile(r"\bexec\s*\(", re.I),
    re.compile(r"\bos\.system\b", re.I),
    re.compile(r"\bsubprocess\b", re.I),
    re.compile(r"\b__import__\b", re.I),
    re.compile(r"file://", re.I),
    re.compile(r"https?://", re.I),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"(^|/|\\)\.\.(/|\\)"),
)


class InMemoryReversibleWriteAdapter:
    """In-process fake store. No network, no filesystem, no credentials."""

    def __init__(
        self,
        *,
        trust_level: str = TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        reversible: bool = True,
        resource_prefix: str = TEST_RESOURCE_PREFIX,
    ):
        self._descriptor = SideEffectToolDescriptor(
            tool_id=TEST_TOOL_ID,
            trust_level=trust_level,
            capabilities_required=(CAP_EXTERNAL_WRITE,),
            reversible=reversible,
            supports_idempotency=True,
            network_access=False,
            operations=(TEST_OPERATION_SET_VALUE,),
            resource_prefix=resource_prefix,
        )
        self._values: dict[str, object] = {}
        self._priors: dict[str, object] = {}
        self._rollbacks_done: set[str] = set()
        self.calls = 0
        self.rollback_calls = 0
        self.received_idempotency_keys: list[str] = []
        self.fail_after_write = False
        self.fail_before_write = False
        self.fail_rollback = False
        self.hang_before_write = False
        self.hang_after_write = False
        self.mutated = False

    @property
    def descriptor(self) -> SideEffectToolDescriptor:
        return self._descriptor

    @property
    def tool_id(self) -> str:
        return self._descriptor.tool_id

    @property
    def trust_level(self) -> str:
        return self._descriptor.trust_level

    @property
    def capabilities_required(self) -> tuple[str, ...]:
        return self._descriptor.capabilities_required

    @property
    def reversible(self) -> bool:
        return self._descriptor.reversible

    @property
    def data(self) -> dict[str, object]:
        return dict(self._values)

    def _validate_input(self, action, context) -> tuple[str, object]:
        payload = dict(getattr(context, "payload", {}) or {})
        if any(callable(item) for item in payload.values()):
            raise SideEffectExecutionError("invalid_input")
        if any(isinstance(item, (dict, list, tuple, set)) for item in payload.values()):
            raise SideEffectExecutionError("invalid_input")
        value = payload.get("value", payload.get("test_value"))
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise SideEffectExecutionError("invalid_input")
        if isinstance(value, str):
            if len(value) > 256:
                raise SideEffectExecutionError("invalid_input")
            for pattern in _FORBIDDEN_VALUE_PATTERNS:
                if pattern.search(value):
                    raise SideEffectExecutionError("invalid_input")
        resource = str(action.resource or "")
        if not resource.startswith(self._descriptor.resource_prefix):
            raise SideEffectExecutionError("resource_out_of_scope")
        return resource, value

    async def execute(self, action, context) -> AdapterExecutionResult:
        if getattr(context, "hang_before_write", False) or self.hang_before_write:
            await asyncio.sleep(3600)
        resource, value = self._validate_input(action, context)
        self.calls += 1
        key = getattr(context, "idempotency_key", None) or action.idempotency_key
        if key:
            self.received_idempotency_keys.append(str(key))
        if self.fail_before_write:
            raise SideEffectExecutionError("adapter_failed_before_write")
        prior = self._values.get(resource)
        self._priors[resource] = prior
        self._values[resource] = value
        self.mutated = True
        reference = "testref-" + hashlib.sha256(
            f"{resource}:{action.action_id}".encode("utf-8")
        ).hexdigest()[:16]
        rollback_reference = "rb-" + str(uuid.uuid4())
        if getattr(context, "hang_after_write", False) or self.hang_after_write:
            await asyncio.sleep(3600)
        if self.fail_after_write:
            raise SideEffectExecutionError("adapter_failed_after_write")
        return AdapterExecutionResult(
            success=True,
            external_reference=reference,
            reversible=self.reversible,
            rollback_reference=rollback_reference,
            metadata={
                **value_fingerprint(value),
                "idempotency_key_received": bool(key),
            },
        )

    async def rollback(self, result, context) -> RollbackResult:
        self.rollback_calls += 1
        if not self.reversible:
            from side_effects.errors import RollbackNotSupportedError

            raise RollbackNotSupportedError()
        reference = getattr(result, "rollback_reference", None) or (
            result.get("rollback_reference") if isinstance(result, dict) else None
        )
        if not reference:
            raise RollbackExecutionError("rollback_reference_missing")
        if reference in self._rollbacks_done:
            return RollbackResult(
                success=True,
                rollback_reference=reference,
                metadata={"duplicate": True},
            )
        if self.fail_rollback:
            raise RollbackExecutionError()
        resource = str(getattr(context, "resource", "") or "")
        if resource in self._priors:
            prior = self._priors[resource]
            if prior is None:
                self._values.pop(resource, None)
            else:
                self._values[resource] = prior
        self._rollbacks_done.add(reference)
        return RollbackResult(success=True, rollback_reference=reference)
