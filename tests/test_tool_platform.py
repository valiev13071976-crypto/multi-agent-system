"""Tool & Integration Platform — registry, router, adapters, credentials."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from autonomy.capabilities import (
    CAP_EXTERNAL_READ,
    CAP_FILESYSTEM_READ,
    CapabilitySet,
)
from autonomy.models import utc_now
from tools.adapters import search_tool_descriptor
from tools.errors import ToolNotFoundError
from tools.gateway import ToolGateway
from tools.integration import IntegrationConfig, IntegrationCredentialStore
from tools.models import (
    TOOL_STATUS_SUCCEEDED,
    ToolRequest,
)
from tools.platform.descriptors import filesystem_read_descriptor
from tools.platform.filesystem import FilesystemAdapter
from tools.platform.http_adapter import HttpAdapter
from tools.platform.scaffold import BitrixAdapter, McpScaffoldAdapter
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry
from tools.router import ToolRouter
from tools.search.fake_provider import FakeSearchProvider


def _caps(*names):
    return CapabilitySet(subject_id="u1", capabilities=names, issued_at=utc_now())


class ToolPlatformRegistryTests(unittest.TestCase):
    def test_capability_lookup_and_unregister(self):
        reg = ToolRegistry()
        reg.register(search_tool_descriptor())
        matches = reg.find_by_capability(CAP_EXTERNAL_READ)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].tool_id, "search")
        reg.unregister("search")
        with self.assertRaises(ToolNotFoundError):
            reg.get("search")

    def test_validate_startup_clean(self):
        reg = ToolRegistry()
        reg.register(search_tool_descriptor())
        router = ToolRouter(reg)
        self.assertEqual(router.validate_startup(), [])
        self.assertEqual(reg.validate_startup(), [])

    def test_platform_bootstrap_registers_tools(self):
        reg = ToolRegistry()
        with tempfile.TemporaryDirectory() as tmp:
            env = {"TOOL_FS_ALLOWED_ROOTS": tmp}
            register_platform_tools(reg, env=env)
            ids = {d.tool_id for d in reg.list_tools(include_disabled=True)}
            self.assertIn("filesystem.read", ids)
            self.assertIn("bitrix.catalog", ids)
            self.assertIn("mcp.invoke", ids)


class ToolPlatformRouterTests(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()
        self.reg.register(search_tool_descriptor())
        self.reg.register(filesystem_read_descriptor(enabled=False))
        self.router = ToolRouter(self.reg)

    def test_explicit_tool_id(self):
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="search",
            operation="search",
        )
        route = self.router.route(req)
        self.assertTrue(route.explicit)
        self.assertEqual(route.tool_id, "search")

    def test_capability_lookup(self):
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="",
            operation="search",
            capability_context=CAP_EXTERNAL_READ,
        )
        route = self.router.route(req, capability=CAP_EXTERNAL_READ)
        self.assertEqual(route.tool_id, "search")
        self.assertTrue(route.capability_match)

    def test_disabled_tool_denied(self):
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="filesystem.read",
            operation="list",
        )
        from tools.errors import ToolDisabledError

        with self.assertRaises(ToolDisabledError):
            self.router.route(req)

    def test_unavailable_adapter(self):
        self.router.set_adapter_health("search", "unavailable")
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="search",
            operation="search",
        )
        from tools.errors import ToolUnavailableError

        with self.assertRaises(ToolUnavailableError):
            self.router.route(req)


class FilesystemAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.sample = self.root / "hello.txt"
        self.sample.write_text("world", encoding="utf-8")
        self.adapter = FilesystemAdapter(allowed_roots=(str(self.root),))

    def _req(self, operation, **arguments):
        return ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id="wf",
            task_id="t",
            tool_id="filesystem.read",
            operation=operation,
            arguments=arguments,
        )

    async def test_read_allowed_file(self):
        data = await self.adapter.execute_read(
            self._req("read", path=str(self.sample)), {}
        )
        self.assertIn("world", data["content_text"])

    async def test_traversal_denied(self):
        from tools.errors import ToolPermissionDeniedError

        with self.assertRaises(ToolPermissionDeniedError):
            await self.adapter.execute_read(
                self._req("read", path=str(self.root / ".." / "etc" / "passwd")), {}
            )

    async def test_write_in_sandbox(self):
        target = self.root / "out.txt"
        data = await self.adapter.execute_write(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id="filesystem.write",
                operation="write",
                arguments={"path": str(target), "content": "ok"},
                idempotency_key="k1",
            ),
            {},
        )
        self.assertTrue(target.exists())
        self.assertEqual(data["written_bytes"], 2)


class HttpAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_forbidden_host_denied(self):
        adapter = HttpAdapter(allowed_hosts=("example.com",))
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="http.request",
            operation="get",
            arguments={"url": "https://not-allowed.example.org/path"},
        )
        from tools.errors import ToolAuthFailedError

        with self.assertRaises(ToolAuthFailedError):
            await adapter.execute_read(req, {})

    async def test_private_network_url_denied(self):
        adapter = HttpAdapter(allowed_hosts=("127.0.0.1",))
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="http.request",
            operation="get",
            arguments={"url": "http://127.0.0.1/secret"},
        )
        from tools.errors import ToolArgumentInvalidError

        with self.assertRaises(ToolArgumentInvalidError):
            await adapter.execute_read(req, {})


class McpScaffoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_mcp_tool_rejected(self):
        adapter = McpScaffoldAdapter()
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="mcp.invoke",
            operation="invoke",
            arguments={"mcp_tool": "_internal"},
        )
        from tools.errors import ToolPolicyDeniedError

        with self.assertRaises(ToolPolicyDeniedError):
            await adapter.execute_read(req, {})

    async def test_disabled_mcp_harmless(self):
        adapter = McpScaffoldAdapter()
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="mcp.invoke",
            operation="invoke",
            arguments={"mcp_tool": "search"},
        )
        from tools.errors import ToolUnavailableError

        with self.assertRaises(ToolUnavailableError):
            await adapter.execute_read(req, {})


class BitrixAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_read_scaffold(self):
        adapter = BitrixAdapter(enabled=True)
        req = ToolRequest(
            request_id="r1",
            workflow_id="wf",
            task_id="t",
            tool_id="bitrix.catalog",
            operation="product_read",
            arguments={"sku": "ABC"},
        )
        data = await adapter.execute_read(req, {})
        self.assertTrue(data.get("scaffold"))
        self.assertEqual(data.get("adapter"), "bitrix")


class CredentialIsolationTests(unittest.TestCase):
    def test_tenant_cannot_use_other_tenant_secret(self):
        store = IntegrationCredentialStore()
        store.register(
            IntegrationConfig(
                integration_id="bitrix-main",
                tenant_id="tenant-a",
                adapter_id="bitrix",
                provider="bitrix",
                enabled=True,
                credential_ref="bitrix-main",
            ),
            secret="secret-a",
        )
        cfg = store.get_config("tenant-b", "bitrix-main")
        self.assertIsNone(cfg)
        from tools.errors import ToolAuthFailedError

        with self.assertRaises(ToolAuthFailedError):
            store.assert_tenant_access("tenant-b", "bitrix-main")


class ToolGatewayPlatformIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = ToolRegistry()
        self.gateway = ToolGateway(
            FakeSearchProvider(),
            registry=self.reg,
            register_search=True,
        )
        register_platform_tools(
            self.reg,
            env={"TOOL_FS_ALLOWED_ROOTS": self.tmp},
            document_service=None,
        )
        self.gateway.router = ToolRouter(self.reg)
        for row in self.reg._items.values():  # noqa: SLF001
            adapter = row.adapter
            if adapter and hasattr(adapter, "health"):
                self.gateway.router.set_adapter_health(
                    getattr(adapter, "adapter_id", ""), adapter.health()
                )

    def _req(self, tool_id, operation, **kwargs):
        base = dict(
            request_id=str(uuid.uuid4()),
            workflow_id="wf",
            task_id="t",
            tool_id=tool_id,
            operation=operation,
            arguments=kwargs.pop("arguments", {}),
            requested_capabilities=kwargs.pop(
                "requested_capabilities", (CAP_FILESYSTEM_READ,)
            ),
        )
        base.update(kwargs)
        return ToolRequest(**base)

    async def test_search_canonical_path(self):
        result = await self.gateway.invoke(
            self._req(
                "search",
                "search",
                arguments={"query": "test", "max_results": 1},
                requested_capabilities=(CAP_EXTERNAL_READ,),
            ),
            capabilities=_caps(CAP_EXTERNAL_READ),
        )
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)

    async def test_filesystem_read_via_gateway(self):
        sample = Path(self.tmp) / "a.txt"
        sample.write_text("hi", encoding="utf-8")
        result = await self.gateway.invoke(
            self._req(
                "filesystem.read",
                "read",
                arguments={"path": str(sample)},
                requested_capabilities=(CAP_FILESYSTEM_READ,),
            ),
            capabilities=_caps(CAP_FILESYSTEM_READ),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.adapter_id, "filesystem")

    async def test_missing_capability_denied(self):
        result = await self.gateway.invoke(
            self._req(
                "filesystem.read",
                "list",
                arguments={"path": self.tmp},
                requested_capabilities=(),
            ),
        )
        self.assertEqual(result.error_code, "missing_tool_capability")

    async def test_document_tool_disabled_without_service(self):
        result = await self.gateway.invoke(
            self._req(
                "document.parse",
                "parse",
                arguments={"document_id": "missing"},
                requested_capabilities=(CAP_FILESYSTEM_READ,),
            ),
            capabilities=_caps(CAP_FILESYSTEM_READ),
        )
        self.assertEqual(result.error_code, "tool_disabled")

    async def test_audit_has_no_secrets(self):
        await self.gateway.invoke(
            self._req(
                "search",
                "search",
                arguments={"query": "ghp_secrettoken123", "max_results": 1},
                requested_capabilities=(CAP_EXTERNAL_READ,),
            ),
            capabilities=_caps(CAP_EXTERNAL_READ),
        )
        blob = str(self.gateway.audit.list_all())
        self.assertNotIn("ghp_secrettoken123", blob)


if __name__ == "__main__":
    unittest.main()
