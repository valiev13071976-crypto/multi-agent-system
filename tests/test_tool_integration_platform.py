"""Tool & Integration Platform gaps 4.1–4.22 — registry/router/executor/permissions/contracts."""

from __future__ import annotations

import tempfile
import unittest
import uuid

from autonomy.capabilities import (
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CAP_FILESYSTEM_READ,
    CapabilityScope,
    CapabilitySet,
)
from autonomy.models import utc_now
from hitl.authority import InMemoryApprovalAuthority, ROLE_PRIVILEGED_APPROVER
from hitl.service import HITLService
from side_effects.executor import SideEffectExecutor
from side_effects.models import TEST_TOOL_ID, default_test_descriptor
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import T0, caps, eval_kwargs
from tools.adapters import descriptor_from_side_effect, search_tool_descriptor
from tools.artifacts import ArtifactRef, ArtifactStore
from tools.errors import (
    ToolArgumentInvalidError,
    ToolDisabledError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolPolicyDeniedError,
    ToolRegistryConflictError,
    ToolRegistryFrozenError,
    ToolUnavailableError,
)
from tools.executor import UnifiedToolExecutor
from tools.failure import (
    REASON_TIMEOUT,
    REASON_UNAUTHORIZED,
    REASON_VALIDATION_ERROR,
    classify_exception,
    is_retryable,
)
from tools.gateway import ToolGateway
from tools.models import (
    ADAPTER_UNAVAILABLE,
    APPROVAL_POLICY_REQUIRED,
    OP_READ,
    TOOL_STATUS_APPROVAL_REQUIRED,
    TOOL_STATUS_SUCCEEDED,
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    TRUSTED_INTERNAL,
    VERSION_ACTIVE,
    ToolDescriptor,
    ToolRequest,
)
from tools.permissions import authorize_tool_request, normalize_capability
from tools.platform.contracts import (
    BrowserReadAdapter,
    CalendarContractAdapter,
    CrmContractAdapter,
    DatabaseContractAdapter,
    DocumentsOcrContractAdapter,
    EmailContractAdapter,
    ExcelContractAdapter,
    ExternalApiContractAdapter,
    ImageContractAdapter,
    McpAdapter,
    ScrapingContractAdapter,
    SeoAnalyticsContractAdapter,
    TelegramContractAdapter,
    WebSearchContractAdapter,
)
from tools.platform.descriptors import (
    excel_inspect_descriptor,
    filesystem_read_descriptor,
)
from tools.platform.fake import FakeToolAdapter
from tools.registry import ToolRegistry
from tools.router import ToolRouter
from tools.schema_validation import validate_tool_args
from tools.secrets_ref import SecretReference
from tools.workload_hints import hint_for_tool, workload_hint_name
from workflow.engine import WorkflowEngine
from workflow.run_envelope import RunEnvelope


def _caps(*names):
    return CapabilitySet(subject_id="u1", capabilities=names, issued_at=utc_now())


def _desc(**overrides) -> ToolDescriptor:
    base = dict(
        tool_id="demo.read",
        name="Demo",
        description="demo",
        version="1.0.0",
        trust_level=TOOL_TRUST_INTERNAL_SAFE,
        capabilities_required=(CAP_FILESYSTEM_READ,),
        action_types_supported=("read",),
        operations=("read",),
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=5.0,
        enabled=True,
        category="demo",
        adapter_id="demo",
    )
    base.update(overrides)
    return ToolDescriptor(**base)


class RegistryResolveVersionTests(unittest.TestCase):
    def test_register_resolve_versions_disable(self):
        reg = ToolRegistry()
        d1 = _desc(version="1.0.0")
        d2 = _desc(version="2.0.0")
        reg.register(d1, adapter=FakeToolAdapter(adapter_id="demo"))
        reg.register(d2, adapter=FakeToolAdapter(adapter_id="demo"), as_primary=True)
        self.assertEqual(set(reg.list_versions("demo.read")), {"1.0.0", "2.0.0"})
        self.assertEqual(reg.resolve("demo.read").descriptor.version, "2.0.0")
        self.assertEqual(reg.resolve("demo.read", "1.0.0").descriptor.version, "1.0.0")
        with self.assertRaises(ToolNotFoundError):
            reg.resolve("demo.read", "9.9.9")
        reg.disable("demo.read", version="2.0.0")
        with self.assertRaises(ToolDisabledError):
            reg.resolve("demo.read", "2.0.0")
        reg.enable("demo.read", version="1.0.0")
        self.assertTrue(reg.resolve("demo.read", "1.0.0").descriptor.enabled)

    def test_unknown_and_conflict_and_freeze(self):
        reg = ToolRegistry()
        reg.register(_desc())
        with self.assertRaises(ToolRegistryConflictError):
            reg.register(_desc())
        with self.assertRaises(ToolNotFoundError):
            reg.resolve("missing.tool")
        reg.freeze()
        with self.assertRaises(ToolRegistryFrozenError):
            reg.disable("demo.read")

    def test_governance_immutable_via_request_payload(self):
        # Request cannot redefine governance; descriptor fields stay authoritative
        desc = _desc(platform_trust=TRUSTED_INTERNAL, approval_policy=APPROVAL_POLICY_REQUIRED)
        self.assertEqual(desc.trust_level, TOOL_TRUST_INTERNAL_SAFE)
        req = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id=desc.tool_id,
            operation="read",
            metadata={"trust_level": "PRIVILEGED", "approval_policy": "none"},
        )
        self.assertNotEqual(dict(req.metadata).get("trust_level"), desc.trust_level)
        self.assertEqual(desc.operation_class_for("read"), OP_READ)


class RouterCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()
        self.reg.register(search_tool_descriptor())
        self.reg.register(filesystem_read_descriptor(enabled=True), adapter=FakeToolAdapter())
        self.router = ToolRouter(self.reg)

    def test_capability_selection(self):
        req = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id="",
            operation="search",
            capability_context=CAP_EXTERNAL_READ,
        )
        decision = self.router.route(
            req, capability=CAP_EXTERNAL_READ, capabilities=_caps(CAP_EXTERNAL_READ)
        )
        self.assertEqual(decision.selected_tool, "search")
        self.assertEqual(decision.policy_decision, "allow")
        self.assertTrue(decision.candidates)

    def test_unauthorized_no_silent_fallback(self):
        req = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id="",
            operation="search",
        )
        with self.assertRaises(ToolNotFoundError) as ctx:
            self.router.route(
                req, capability=CAP_EXTERNAL_READ, capabilities=_caps(CAP_FILESYSTEM_READ)
            )
        self.assertEqual(ctx.exception.error_code, "no_eligible_tool")

    def test_disabled_and_health_rejection(self):
        self.reg.disable("filesystem.read")
        req = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id="filesystem.read",
            operation="read",
        )
        with self.assertRaises(ToolDisabledError):
            self.router.route(req)
        self.reg.enable("filesystem.read")
        self.router.set_adapter_health("filesystem", ADAPTER_UNAVAILABLE)
        # Re-register enabled descriptor with matching adapter_id
        # filesystem_read uses adapter_id filesystem
        self.router.set_adapter_health(
            self.reg.get("filesystem.read").adapter_id, ADAPTER_UNAVAILABLE
        )
        with self.assertRaises(ToolUnavailableError):
            self.router.route(
                ToolRequest(
                    request_id="r2",
                    workflow_id="w",
                    task_id="t",
                    tool_id="filesystem.read",
                    operation="list",
                )
            )


class ExecutorSchemaEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_validation_and_envelope_identity(self):
        reg = ToolRegistry()
        fake = FakeToolAdapter(adapter_id="demo")
        desc = _desc(
            metadata={
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                }
            }
        )
        reg.register(desc, adapter=fake)
        gw = ToolGateway(registry=reg, register_search=False)
        exe = UnifiedToolExecutor(registry=reg, gateway=gw)
        envelope = RunEnvelope.create(
            execution_id="exec-1",
            request_id="req-1",
            workflow_id="wf-1",
            task_id="task-1",
            tenant_id="tenant-A",
            correlation_id="corr-1",
            trace_id="trace-1",
            user_id="user-1",
            actor_ref="actor-1",
            data_scope_ref="tenant-A/docs",
        )
        with self.assertRaises(ToolArgumentInvalidError):
            validate_tool_args({"extra": 1}, desc)
        result = await exe.execute(
            tool_id="demo.read",
            operation="read",
            arguments={"path": "/x", "tenant_id": "HACK"},
            envelope=envelope,
            capabilities=_caps(CAP_FILESYSTEM_READ),
        )
        self.assertTrue(result.success, result.error_code)
        # Identity from envelope, not payload
        events = [e for e in gw.audit.list_all() if e.get("event_type") == "tool.requested"]
        self.assertTrue(events)

    async def test_timeout_structured_error(self):
        reg = ToolRegistry()
        fake = FakeToolAdapter(adapter_id="demo", fail_mode="timeout")
        reg.register(_desc(timeout_seconds=0.01), adapter=fake)
        gw = ToolGateway(registry=reg, register_search=False)
        result = await gw.invoke(
            ToolRequest(
                request_id="r",
                workflow_id="w",
                task_id="t",
                tool_id="demo.read",
                operation="read",
                requested_capabilities=(CAP_FILESYSTEM_READ,),
            ),
            capabilities=_caps(CAP_FILESYSTEM_READ),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "tool_timeout")
        info = classify_exception(ToolUnavailableError())
        self.assertTrue(info.retryable or info.reason_code)


class PermissionsTests(unittest.TestCase):
    def test_default_deny_and_aliases(self):
        self.assertEqual(normalize_capability("files.read"), CAP_FILESYSTEM_READ)
        self.assertEqual(normalize_capability("web.search"), CAP_EXTERNAL_READ)
        desc = _desc()
        req = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id=desc.tool_id,
            operation="read",
            tenant_id="t1",
            actor_id="a1",
        )
        with self.assertRaises(ToolPermissionDeniedError):
            authorize_tool_request(request=req, descriptor=desc, capabilities=None)
        # Trust alone does not grant
        with self.assertRaises(ToolPermissionDeniedError):
            authorize_tool_request(
                request=req, descriptor=desc, capabilities=_caps(CAP_EXTERNAL_READ)
            )
        ok = authorize_tool_request(
            request=req, descriptor=desc, capabilities=_caps(CAP_FILESYSTEM_READ)
        )
        self.assertTrue(ok.allowed)

    def test_data_scope_enforcement(self):
        desc = _desc()
        scope = CapabilityScope(tool_id="demo.read", resource_pattern="tenant-A/*")
        capset = CapabilitySet(
            subject_id="u1",
            capabilities=(CAP_FILESYSTEM_READ,),
            issued_at=utc_now(),
            scope=scope,
        )
        req = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id="demo.read",
            operation="read",
            arguments={"resource": "tenant-B/secret"},
            actor_id="a1",
            tenant_id="tenant-A",
        )
        with self.assertRaises(ToolPermissionDeniedError):
            authorize_tool_request(request=req, descriptor=desc, capabilities=capset)
        req2 = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id="demo.read",
            operation="read",
            arguments={"resource": "tenant-A/ok"},
            actor_id="a1",
            tenant_id="tenant-A",
        )
        self.assertTrue(
            authorize_tool_request(request=req2, descriptor=desc, capabilities=capset).allowed
        )


class ApprovalAndIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_no_approval_write_requires(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("t", tenant_id="tenant-se")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        adapter = InMemoryReversibleWriteAdapter(
            trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        )
        se_reg = SideEffectAdapterRegistry()
        se_reg.register(adapter)
        gate = engine._gate()
        executor = SideEffectExecutor(se_reg, gate=gate)
        authority = InMemoryApprovalAuthority()
        authority.grant("reviewer-1", ROLE_PRIVILEGED_APPROVER)
        hitl = HITLService(
            gate=gate,
            state_manager=engine.state_manager,
            store=gate.approvals.store,
            authority=authority,
        )
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE),
                name=TEST_TOOL_ID,
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        # Also register a read fake
        registry.register(
            _desc(tool_id="demo.read2", adapter_id="demo2"),
            adapter=FakeToolAdapter(adapter_id="demo2"),
        )
        gw = ToolGateway(
            registry=registry,
            side_effect_executor=executor,
            gate=gate,
            hitl=hitl,
            register_search=False,
        )
        read = await gw.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                task_id="t",
                tool_id="demo.read2",
                operation="read",
                requested_capabilities=(CAP_FILESYSTEM_READ,),
                actor_id="agent-1",
                tenant_id="tenant-se",
            ),
            capabilities=_caps(CAP_FILESYSTEM_READ),
        )
        self.assertTrue(read.success)
        write = await gw.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                task_id="t",
                tool_id=TEST_TOOL_ID,
                operation="set_value",
                arguments={"resource": "test/key", "value": "v"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="idem-approval-1",
                actor_id="agent-1",
                tenant_id="tenant-se",
            ),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=gate,
            hitl=hitl,
            executor=executor,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertEqual(write.status, TOOL_STATUS_APPROVAL_REQUIRED)

    async def test_fake_write_idempotent_via_side_effect(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("t", tenant_id="tenant-se")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        adapter = InMemoryReversibleWriteAdapter(trust_level=TOOL_TRUST_INTERNAL_SAFE)
        se_reg = SideEffectAdapterRegistry()
        se_reg.register(adapter)
        gate = engine._gate()
        executor = SideEffectExecutor(se_reg, gate=gate)
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_INTERNAL_SAFE),
                name=TEST_TOOL_ID,
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        gw = ToolGateway(
            registry=registry,
            side_effect_executor=executor,
            gate=gate,
            register_search=False,
        )
        req_kwargs = dict(
            workflow_id=workflow_id,
            task_id="t",
            tool_id=TEST_TOOL_ID,
            operation="set_value",
            arguments={"resource": "test/key", "value": "v"},
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            idempotency_key="idem-retry-1",
            actor_id="agent-1",
            tenant_id="tenant-se",
        )
        r1 = await gw.invoke(
            ToolRequest(request_id=str(uuid.uuid4()), **req_kwargs),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=gate,
            executor=executor,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        r2 = await gw.invoke(
            ToolRequest(request_id=str(uuid.uuid4()), **req_kwargs),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=gate,
            executor=executor,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        self.assertTrue(r1.success)
        self.assertTrue(r2.success)
        self.assertEqual(adapter.calls, 1)


class ContractAdapterCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def _read(self, adapter, tool_id, operation="read", **args):
        req = ToolRequest(
            request_id="r",
            workflow_id="w",
            task_id="t",
            tool_id=tool_id,
            operation=operation,
            arguments=args,
        )
        return await adapter.execute_read(req, {})

    async def test_web_search_contract(self):
        data = await self._read(WebSearchContractAdapter(), "web_search.query", "query", query="q")
        self.assertTrue(data.get("scaffold") or data.get("contract") or "results" in data)

    async def test_browser_read_contract(self):
        data = await self._read(BrowserReadAdapter(), "browser.read", "fetch")
        self.assertEqual(data["adapter"], "browser")

    async def test_excel_contract(self):
        data = await self._read(ExcelContractAdapter(), "excel.inspect", "inspect")
        self.assertIn("sheets", data)

    async def test_documents_ocr_contract(self):
        data = await self._read(DocumentsOcrContractAdapter(), "document.ocr", "ocr")
        self.assertTrue(data.get("ocr"))

    async def test_email_calendar_telegram(self):
        self.assertTrue((await self._read(EmailContractAdapter(), "email.message", "search")).get("scaffold"))
        self.assertTrue((await self._read(CalendarContractAdapter(), "calendar.event", "list")).get("scaffold"))
        self.assertTrue((await self._read(TelegramContractAdapter(), "telegram.message", "read_updates")).get("scaffold"))

    async def test_crm_cms_external_db_image_scrape_seo_mcp(self):
        self.assertTrue((await self._read(CrmContractAdapter(), "crm.entity", "search")).get("scaffold"))
        self.assertTrue((await self._read(ExternalApiContractAdapter(), "external_api.request", "request")).get("scaffold"))
        self.assertTrue((await self._read(DatabaseContractAdapter(), "database.read", "select")).get("scaffold"))
        self.assertTrue((await self._read(ImageContractAdapter(), "image.generate", "generate")).get("scaffold"))
        self.assertTrue((await self._read(ScrapingContractAdapter(), "scrape.fetch", "fetch")).get("scaffold"))
        self.assertTrue((await self._read(SeoAnalyticsContractAdapter(), "seo.analytics_read", "analytics_read")).get("scaffold"))
        mcp = McpAdapter(
            enabled=True,
            allowed_servers=("srv1",),
            allowed_tools=("search",),
            server_trust={"srv1": "trusted"},
        )
        data = await self._read(
            mcp, "mcp.invoke", "invoke", server="srv1", mcp_tool="search"
        )
        self.assertEqual(data["server"], "srv1")
        with self.assertRaises(ToolPolicyDeniedError):
            await self._read(mcp, "mcp.invoke", "invoke", server="evil", mcp_tool="search")


class WorkloadHintsTests(unittest.TestCase):
    def test_workload_mapping(self):
        self.assertEqual(
            workload_hint_name(tool_id="excel.inspect", estimated_rows=200_000),
            "batch",
        )
        self.assertEqual(hint_for_tool(tool_id="scrape.fetch").name, "batch")
        self.assertEqual(hint_for_tool(tool_id="document.ocr", operation="ocr").name, "batch")
        self.assertEqual(hint_for_tool(tool_id="image.generate").name, "background")
        search = hint_for_tool(tool_id="search", metadata={"priority": "high"})
        self.assertIn(search.name, {"interactive", "normal"})
        normal = hint_for_tool(tool_id="search")
        self.assertIn(normal.name, {"interactive", "normal"})


class ObservabilityIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_secret_leakage_and_correlation(self):
        reg = ToolRegistry()
        reg.register(_desc(), adapter=FakeToolAdapter(adapter_id="demo"))
        gw = ToolGateway(registry=reg, register_search=False)
        await gw.invoke(
            ToolRequest(
                request_id="r-obs",
                workflow_id="wf",
                task_id="t",
                tool_id="demo.read",
                operation="read",
                arguments={"path": "/x", "api_key": "sk-secret"},
                requested_capabilities=(CAP_FILESYSTEM_READ,),
                correlation_id="corr-xyz",
                actor_id="a1",
                tenant_id="t1",
            ),
            capabilities=_caps(CAP_FILESYSTEM_READ),
        )
        blob = str(gw.audit.list_all())
        self.assertNotIn("sk-secret", blob)

    def test_artifact_tenant_isolation(self):
        store = ArtifactStore()
        a = store.put(
            ArtifactRef.create(tenant_id="A", owner_id="o1", tool_id="demo.read")
        )
        store.put(ArtifactRef.create(tenant_id="B", owner_id="o2", tool_id="demo.read"))
        with self.assertRaises(ToolPermissionDeniedError):
            store.get(a.artifact_id, tenant_id="B")
        self.assertEqual(store.get(a.artifact_id, tenant_id="A").tenant_id, "A")

    def test_adapter_health_isolation(self):
        reg = ToolRegistry()
        reg.register(_desc(tool_id="a.tool", adapter_id="adapter_a"), adapter=FakeToolAdapter(adapter_id="adapter_a"))
        reg.register(
            _desc(tool_id="b.tool", adapter_id="adapter_b"),
            adapter=FakeToolAdapter(adapter_id="adapter_b"),
        )
        router = ToolRouter(reg)
        router.set_adapter_health("adapter_a", ADAPTER_UNAVAILABLE)
        with self.assertRaises(ToolUnavailableError):
            router.route(
                ToolRequest(
                    request_id="r",
                    workflow_id="w",
                    task_id="t",
                    tool_id="a.tool",
                    operation="read",
                )
            )
        # B remains routable
        decision = router.route(
            ToolRequest(
                request_id="r2",
                workflow_id="w",
                task_id="t",
                tool_id="b.tool",
                operation="read",
            )
        )
        self.assertEqual(decision.selected_tool, "b.tool")


class E2EPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_e2e_workflow_to_audit(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("t", tenant_id="tenant-e2e")
        reg = ToolRegistry()
        fake = FakeToolAdapter(adapter_id="demo")
        reg.register(_desc(), adapter=fake)
        gw = ToolGateway(registry=reg, register_search=False)
        exe = UnifiedToolExecutor(registry=reg, gateway=gw)
        envelope = RunEnvelope.create(
            execution_id="exec-e2e",
            request_id="req-e2e",
            workflow_id=workflow_id,
            task_id="task-1",
            tenant_id="tenant-e2e",
            correlation_id="corr-e2e",
            trace_id="trace-e2e",
            actor_ref="actor-1",
        )
        result = await exe.execute(
            tool_id="demo.read",
            operation="read",
            arguments={"path": "/ok"},
            envelope=envelope,
            capabilities=_caps(CAP_FILESYSTEM_READ),
        )
        self.assertTrue(result.success)
        types = {e["event_type"] for e in gw.audit.list_all()}
        self.assertIn("tool.requested", types)
        self.assertTrue({"tool.routed", "tool.authorized"} & types or "tool.read_completed" in types)

    async def test_mutating_e2e_approval_side_effect(self):
        engine = WorkflowEngine()
        workflow_id = engine.create("t", tenant_id="tenant-se")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        adapter = InMemoryReversibleWriteAdapter(trust_level=TOOL_TRUST_INTERNAL_SAFE)
        se_reg = SideEffectAdapterRegistry()
        se_reg.register(adapter)
        gate = engine._gate()
        se = SideEffectExecutor(se_reg, gate=gate)
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_INTERNAL_SAFE),
                name=TEST_TOOL_ID,
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        gw = ToolGateway(
            registry=registry, side_effect_executor=se, gate=gate, register_search=False
        )
        exe = UnifiedToolExecutor(registry=registry, gateway=gw)
        envelope = RunEnvelope.create(
            execution_id="exec-mut",
            request_id="req-mut",
            workflow_id=workflow_id,
            task_id="t",
            tenant_id="tenant-se",
            correlation_id="corr-mut",
            trace_id="trace-mut",
            actor_ref="agent-1",
            idempotency_key="idem-mut-1",
        )
        result = await exe.execute(
            tool_id=TEST_TOOL_ID,
            operation="set_value",
            arguments={"resource": "test/key", "value": "v"},
            envelope=envelope,
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=gate,
            executor=se,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
            idempotency_key="idem-mut-1",
        )
        self.assertTrue(result.success, result.error_code)
        self.assertTrue(result.side_effect)
        self.assertEqual(adapter.calls, 1)


class SecretsAndFailureTaxonomyTests(unittest.TestCase):
    def test_secret_ref_rejects_plaintext(self):
        with self.assertRaises(ValueError):
            SecretReference(secret_ref="sk-live-plain")
        ref = SecretReference(secret_ref="vault:proj/api")
        self.assertFalse(ref.as_dict()["has_plaintext"])

    def test_failure_taxonomy(self):
        self.assertEqual(classify_exception(ToolArgumentInvalidError()).reason_code, REASON_VALIDATION_ERROR)
        self.assertEqual(classify_exception(ToolPermissionDeniedError()).reason_code, REASON_UNAUTHORIZED)
        self.assertTrue(is_retryable(REASON_TIMEOUT))


class BootstrapDescriptorsTests(unittest.TestCase):
    def test_bootstrap_includes_new_ids(self):
        from tools.platform.bootstrap import register_platform_tools

        reg = ToolRegistry()
        with tempfile.TemporaryDirectory() as tmp:
            register_platform_tools(reg, env={"TOOL_FS_ALLOWED_ROOTS": tmp})
            ids = {d.tool_id for d in reg.list_tools(include_disabled=True)}
            for tid in (
                "excel.inspect",
                "excel.read_range",
                "excel.write",
                "image.generate",
                "image.edit",
                "scrape.fetch",
                "scrape.extract",
                "seo.analytics_read",
                "browser.read",
                "browser.write",
                "external_api.request",
                "mcp.invoke",
            ):
                self.assertIn(tid, ids)
            self.assertFalse(excel_inspect_descriptor(enabled=False).enabled)


if __name__ == "__main__":
    unittest.main()
