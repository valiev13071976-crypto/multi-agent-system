import unittest
import uuid

from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_EXTERNAL_WRITE, CapabilitySet
from autonomy.models import utc_now
from side_effects.github.models import GITHUB_TOOL_ID
from tools.adapters import github_issue_labels_descriptor
from tools.gateway import ToolGateway
from tools.models import FORBIDDEN_BYPASS_KEYS, FORBIDDEN_DYNAMIC_KEYS, ToolRequest
from tools.registry import ToolRegistry
from tools.search.fake_provider import FakeSearchProvider


class ToolGatewaySecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_bypass_keys_accepted(self):
        gateway = ToolGateway(FakeSearchProvider())
        caps = CapabilitySet(
            subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now()
        )
        for key in sorted(FORBIDDEN_BYPASS_KEYS):
            result = await gateway.invoke(
                ToolRequest(
                    request_id=str(uuid.uuid4()),
                    workflow_id="wf",
                    task_id="t",
                    tool_id="search",
                    operation="search",
                    arguments={"query": "x", key: True},
                    requested_capabilities=(CAP_EXTERNAL_READ,),
                ),
                capabilities=caps,
            )
            self.assertEqual(result.error_code, "tool_policy_denied", key)

    async def test_no_dynamic_code_keys(self):
        gateway = ToolGateway(FakeSearchProvider())
        for key in ("module_path", "python_code", "shell_command", "base_url"):
            self.assertIn(key, FORBIDDEN_DYNAMIC_KEYS)
            result = await gateway.invoke(
                ToolRequest(
                    request_id=str(uuid.uuid4()),
                    workflow_id="wf",
                    task_id="t",
                    tool_id="search",
                    operation="search",
                    arguments={"query": "x", key: "evil"},
                    requested_capabilities=(CAP_EXTERNAL_READ,),
                )
            )
            self.assertEqual(result.error_code, "tool_argument_invalid", key)

    async def test_secrets_not_in_audit(self):
        gateway = ToolGateway(FakeSearchProvider())
        caps = CapabilitySet(
            subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now()
        )
        await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id="search",
                operation="search",
                arguments={"query": "hello"},
                requested_capabilities=(CAP_EXTERNAL_READ,),
                metadata={"note": "no_token"},
            ),
            capabilities=caps,
        )
        blob = str(gateway.audit.list_all())
        for needle in (
            "GITHUB_WRITE_TOKEN",
            "PANDA_ENCRYPTION_KEY",
            "Authorization",
            "Bearer ",
        ):
            self.assertNotIn(needle, blob)

    async def test_disabled_write_tool_denied(self):
        registry = ToolRegistry()
        registry.register(github_issue_labels_descriptor(enabled=False))
        gateway = ToolGateway(registry=registry, register_search=False)
        result = await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id=GITHUB_TOOL_ID,
                operation="ensure_label_present",
                arguments={"resource": "github://o/r#1", "label": "x"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="k1",
            )
        )
        self.assertEqual(result.error_code, "tool_disabled")


if __name__ == "__main__":
    unittest.main()
