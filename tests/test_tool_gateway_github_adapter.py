import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_WRITE
from side_effects.github.models import GITHUB_OPERATIONS, GITHUB_TOOL_ID
from tools.adapters import github_issue_labels_descriptor
from tools.gateway import ToolGateway
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, ToolRequest
from tools.registry import ToolRegistry


class ToolGatewayGitHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_contains_github_descriptor(self):
        registry = ToolRegistry()
        registry.register(github_issue_labels_descriptor(enabled=False))
        desc = registry.get(GITHUB_TOOL_ID)
        self.assertEqual(desc.trust_level, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE)
        self.assertEqual(desc.operations, tuple(GITHUB_OPERATIONS))
        self.assertEqual(
            desc.operations, ("ensure_label_present", "ensure_label_absent")
        )

    async def test_unknown_github_operation_denied(self):
        registry = ToolRegistry()
        registry.register(github_issue_labels_descriptor(enabled=True))
        gateway = ToolGateway(registry=registry, register_search=False)
        result = await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id=GITHUB_TOOL_ID,
                operation="create_pull_request",
                arguments={"resource": "github://o/r#1"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="gh-1",
            )
        )
        self.assertEqual(result.error_code, "tool_operation_not_allowed")


if __name__ == "__main__":
    unittest.main()
