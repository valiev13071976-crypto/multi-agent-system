import unittest

from tools.adapters import github_issue_labels_descriptor, search_tool_descriptor
from tools.errors import ToolNotFoundError, ToolRegistryConflictError, ToolRegistryFrozenError
from tools.models import (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    ToolDescriptor,
)
from tools.registry import ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def test_register_get_list(self):
        reg = ToolRegistry()
        reg.register(search_tool_descriptor())
        self.assertEqual(reg.get("search").tool_id, "search")
        self.assertEqual(len(reg.list_tools()), 1)
        self.assertEqual(reg.list_operations("search"), ("search",))

    def test_duplicate_tool_id_denied(self):
        reg = ToolRegistry()
        reg.register(search_tool_descriptor())
        with self.assertRaises(ToolRegistryConflictError):
            reg.register(search_tool_descriptor())

    def test_invalid_trust_denied(self):
        with self.assertRaises(ValueError):
            ToolDescriptor(
                tool_id="x",
                name="x",
                description="x",
                version="1",
                trust_level="NOT_A_TRUST",
                capabilities_required=(),
                action_types_supported=("read",),
                operations=("op",),
                read_only=True,
                reversible=True,
                idempotency_required=False,
                timeout_seconds=1.0,
            )

    def test_version_required(self):
        with self.assertRaises(ValueError):
            ToolDescriptor(
                tool_id="x",
                name="x",
                description="x",
                version="",
                trust_level=TOOL_TRUST_INTERNAL_SAFE,
                capabilities_required=(),
                action_types_supported=("read",),
                operations=("op",),
                read_only=True,
                reversible=True,
                idempotency_required=False,
                timeout_seconds=1.0,
            )

    def test_operation_allowlist_and_freeze(self):
        reg = ToolRegistry()
        reg.register(search_tool_descriptor())
        reg.freeze()
        with self.assertRaises(ToolRegistryFrozenError):
            reg.register(github_issue_labels_descriptor(enabled=False))
        with self.assertRaises(ToolNotFoundError):
            reg.get("missing")

    def test_disabled_filtered(self):
        reg = ToolRegistry()
        reg.register(github_issue_labels_descriptor(enabled=False))
        self.assertEqual(len(reg.list_tools()), 0)
        self.assertEqual(len(reg.list_tools(include_disabled=True)), 1)

    def test_privileged_descriptor_ok_but_default_deny_path_elsewhere(self):
        desc = ToolDescriptor(
            tool_id="priv",
            name="priv",
            description="priv",
            version="1.0.0",
            trust_level=TOOL_TRUST_PRIVILEGED,
            capabilities_required=(),
            action_types_supported=("permission_change",),
            operations=("noop",),
            read_only=False,
            reversible=False,
            idempotency_required=True,
            timeout_seconds=1.0,
            enabled=False,
        )
        self.assertEqual(desc.trust_level, TOOL_TRUST_PRIVILEGED)
        self.assertEqual(search_tool_descriptor().trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)


if __name__ == "__main__":
    unittest.main()
