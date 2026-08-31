"""Reusable adapter conformance helpers for Tool Platform tests."""

from __future__ import annotations

import unittest
from typing import Callable

from tools.errors import ToolError
from tools.invocation import invocation_from_request
from tools.models import ToolDescriptor, ToolRequest
from tools.registry import ToolRegistry
from tools.schema_validation import validate_tool_args
from tools.secrets_ref import SecretReference


def assert_descriptor_valid(descriptor: ToolDescriptor) -> None:
    """Registration-time contract: descriptor must be self-consistent."""
    if not descriptor.tool_id or not descriptor.version:
        raise AssertionError("tool_id_and_version_required")
    if not descriptor.operations:
        raise AssertionError("operations_required")


def assert_secret_safe_metadata(metadata: dict) -> None:
    for key, value in metadata.items():
        lowered = str(key).lower()
        if any(token in lowered for token in ("secret", "password", "api_key", "token")):
            if value and str(value).strip():
                raise AssertionError(f"unsafe_metadata_key:{key}")


def assert_invocation_context(invocation, *, tenant_id: str) -> None:
    if invocation.tenant_id != tenant_id:
        raise AssertionError("tenant_context_lost")


class AdapterContractTestCase(unittest.TestCase):
    """Mixin-style base for adapter conformance."""

    __test__ = False

    descriptor_factory: Callable[[], ToolDescriptor]
    adapter_factory: Callable[[], object]

    def test_descriptor_contract(self):
        desc = self.descriptor_factory()
        assert_descriptor_valid(desc)
        assert_secret_safe_metadata(dict(desc.metadata or {}))

    def test_schema_validation_hook(self):
        desc = self.descriptor_factory()
        schema = dict((desc.metadata or {}).get("input_schema") or {})
        if schema:
            validate_tool_args({"probe": True}, schema)

    def test_invocation_envelope(self):
        desc = self.descriptor_factory()
        req = ToolRequest(
            request_id="req-1",
            workflow_id="wf-1",
            task_id="t-1",
            tool_id=desc.tool_id,
            operation=desc.operations[0],
            tenant_id="tenant-a",
            user_id="user-1",
            actor_id="agent-1",
            tool_version=desc.version,
        )
        inv = invocation_from_request(req)
        assert_invocation_context(inv, tenant_id="tenant-a")
        self.assertEqual(inv.tool_id, desc.tool_id)


def register_for_contract_test(
    registry: ToolRegistry,
    descriptor: ToolDescriptor,
    adapter: object,
) -> None:
    assert_descriptor_valid(descriptor)
    registry.register(descriptor, adapter=adapter)


def expect_tool_error(callable_obj, error_type: type[ToolError]) -> None:
    try:
        callable_obj()
    except error_type:
        return
    except Exception as exc:
        raise AssertionError(f"expected {error_type.__name__}, got {type(exc).__name__}") from exc
    raise AssertionError(f"expected {error_type.__name__}, no exception raised")
