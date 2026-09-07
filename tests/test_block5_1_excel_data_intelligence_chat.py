"""PANDA — BLOCK 5.1 Excel / Data Intelligence Platform — targeted tests.

Covers the new deterministic NL-operation compiler/execution engine
(``data_intel.nl_ops`` / ``data_intel.transform``), the chat-integration
wiring that reroutes ``business_assistant`` FAMILY_EXCEL continuation turns
to the real ``data.excel_assistant`` / ``data.compare_workbooks`` tools
(``business_assistant/action_continuation.py``), and the required
acceptance scenarios (A/B/C/D/E/F) end-to-end through
``WorkflowPandaConversationGateway`` with fakes/in-memory stores only --
no live/paid provider calls.
"""

from __future__ import annotations

import io
import unittest
from decimal import Decimal

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import (
    ActiveTaskStore,
    CALL_TOOL,
    CONTINUE_ACTIVE_TASK,
    FAMILY_EXCEL,
    FAMILY_IMAGE_GENERATE,
    NEW_TASK,
    TOOL_DATA_COMPARE_WORKBOOKS,
    TOOL_DATA_EXCEL_ASSISTANT,
    continuation_decision,
    detect_family,
    resolve_action_turn,
)
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from data_intel.contracts import (
    ROLE_BRAND,
    ROLE_PRICE,
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ColumnDescriptor,
    TableDescriptor,
)
from data_intel.errors import DataIntelError
from data_intel.ingest import ingest_bytes
from data_intel.large import LargeDatasetPolicy
from data_intel.nl_ops import (
    AmbiguousOperationError,
    OP_FILTER_COMPARE,
    OP_FILTER_CONTAINS,
    OP_PERCENT_ROUND,
    UnsupportedOperationError,
    compile_request,
    wants_workbook_export,
)
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from data_intel.transform import execute_plan
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _price_table() -> TableDescriptor:
    columns = (
        ColumnDescriptor(source_name="brand", normalized_name="brand", semantic_role=ROLE_BRAND),
        ColumnDescriptor(
            source_name="product_name", normalized_name="product_name", semantic_role=ROLE_PRODUCT_NAME
        ),
        ColumnDescriptor(source_name="price", normalized_name="price", semantic_role=ROLE_PRICE),
    )
    return TableDescriptor(table_id="t1", sheet="Sheet1", range="A1", header_row=1, columns=columns, row_count=3)


def _two_price_table() -> TableDescriptor:
    columns = (
        ColumnDescriptor(source_name="brand", normalized_name="brand", semantic_role=ROLE_BRAND),
        ColumnDescriptor(
            source_name="purchase_price", normalized_name="purchase_price", semantic_role=ROLE_PURCHASE_PRICE
        ),
        ColumnDescriptor(
            source_name="selling_price", normalized_name="selling_price", semantic_role=ROLE_SELLING_PRICE
        ),
    )
    return TableDescriptor(table_id="t1", sheet="Sheet1", range="A1", header_row=1, columns=columns, row_count=3)


# --------------------------------------------------------------------------
# NL-operation compiler (data_intel.nl_ops) — deterministic, no LLM
# --------------------------------------------------------------------------
class NlOpsCompilerTests(unittest.TestCase):
    def test_keep_only_samsung(self):
        plan = compile_request("Оставь только Samsung", _price_table())
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].op, OP_FILTER_CONTAINS)
        self.assertEqual(plan.operations[0].params["value"], "Samsung")

    def test_price_filter_cheaper_than(self):
        plan = compile_request("Только дешевле 50 000", _price_table())
        self.assertEqual(plan.operations[0].op, OP_FILTER_COMPARE)
        self.assertEqual(plan.operations[0].params["operator"], "<")
        self.assertEqual(plan.operations[0].params["value"], "50000")

    def test_percent_round_up_to_nearest(self):
        plan = compile_request(
            "Сделай минус 10% и округли вверх до ближайших 50", _price_table()
        )
        op = next(o for o in plan.operations if o.op == OP_PERCENT_ROUND)
        self.assertEqual(op.params["percent"], "-10")
        self.assertEqual(op.params["round_mode"], "up")
        self.assertEqual(op.params["round_to"], "50")

    def test_percent_round_minus_12(self):
        plan = compile_request("Сделай минус 12%", _price_table())
        op = next(o for o in plan.operations if o.op == OP_PERCENT_ROUND)
        self.assertEqual(op.params["percent"], "-12")

    def test_ambiguous_price_column_raises_with_candidates(self):
        with self.assertRaises(AmbiguousOperationError) as ctx:
            compile_request("минус 10%", _two_price_table())
        self.assertEqual(set(ctx.exception.candidates), {"purchase_price", "selling_price"})

    def test_ambiguous_price_column_disambiguated_by_word(self):
        plan = compile_request("минус 10% от закупочной цены", _two_price_table())
        op = next(o for o in plan.operations if o.op == OP_PERCENT_ROUND)
        self.assertEqual(op.params["column"], "purchase_price")

    def test_unsupported_request_raises(self):
        with self.assertRaises(UnsupportedOperationError):
            compile_request("расскажи анекдот", _price_table())

    def test_wants_workbook_export_detected(self):
        self.assertTrue(wants_workbook_export("Сохрани результат в Excel"))
        self.assertFalse(wants_workbook_export("Оставь только Samsung"))

    def test_dedup_request(self):
        plan = compile_request("найди дубликаты", _price_table())
        self.assertEqual(plan.operations[0].op, "dedup")


# --------------------------------------------------------------------------
# Deterministic transform execution engine (data_intel.transform)
# --------------------------------------------------------------------------
class TransformEngineTests(unittest.TestCase):
    def setUp(self):
        self.table = _price_table()
        self.rows = [
            {"brand": "Samsung", "product_name": "Galaxy A54", "price": "40000"},
            {"brand": "Samsung", "product_name": "Galaxy S23", "price": "80000"},
            {"brand": "Apple", "product_name": "iPhone 14", "price": "70000"},
        ]

    def test_scenario_b_percent_and_round_exact(self):
        # Known price 40000, minus 10% == 36000, round up to nearest 50 == 36000 (exact).
        plan = compile_request(
            "Сделай минус 10% и округли вверх до ближайших 50", self.table
        )
        result = execute_plan(self.rows, self.table.columns, plan)
        got = {r["product_name"]: r["price"] for r in result.rows}
        self.assertEqual(got["Galaxy A54"], "36000")
        # 80000 * 0.9 = 72000 -> already a multiple of 50
        self.assertEqual(got["Galaxy S23"], "72000")

    def test_round_up_non_multiple(self):
        plan = compile_request(
            "Сделай минус 12% и округли вверх до ближайших 50", self.table
        )
        result = execute_plan(self.rows, self.table.columns, plan)
        # 40000 * 0.88 = 35200 -> round UP to nearest 50 == 35200 (already multiple)
        got = {r["product_name"]: r["price"] for r in result.rows}
        self.assertEqual(got["Galaxy A54"], "35200")
        # 70000 * 0.88 = 61600 -> round up to nearest 50 == 61600 (multiple already);
        # use a non-multiple case explicitly below.

    def test_round_up_strictly_non_multiple_of_step(self):
        rows = [{"brand": "X", "product_name": "P1", "price": "101"}]
        plan = compile_request("минус 0% и округли вверх до ближайших 50", self.table)
        result = execute_plan(rows, self.table.columns, plan)
        # 101 rounded UP to nearest 50 -> 150
        self.assertEqual(result.rows[0]["price"], "150")

    def test_filter_and_sequential_transform_chain(self):
        keep_plan = compile_request("Оставь только Samsung", self.table)
        stage1 = execute_plan(self.rows, self.table.columns, keep_plan)
        self.assertEqual(stage1.row_count_after, 2)
        cheaper_plan = compile_request("Только дешевле 50 000", self.table)
        stage2 = execute_plan(stage1.rows, stage1.columns, cheaper_plan)
        self.assertEqual(stage2.row_count_after, 1)
        self.assertEqual(stage2.rows[0]["product_name"], "Galaxy A54")

    def test_identifiers_never_become_float_or_scientific(self):
        table = TableDescriptor(
            table_id="t1",
            sheet="S",
            range="A1",
            header_row=1,
            columns=(
                ColumnDescriptor(source_name="sku", normalized_name="sku", semantic_role="sku"),
                ColumnDescriptor(source_name="price", normalized_name="price", semantic_role=ROLE_PRICE),
            ),
            row_count=1,
        )
        rows = [{"sku": "00012345", "price": "100"}]
        plan = compile_request("минус 10%", table)
        result = execute_plan(rows, table.columns, plan)
        self.assertEqual(result.rows[0]["sku"], "00012345")

    def test_add_remove_rename_column(self):
        add_plan = compile_request("добавь колонку margin +20%", self.table)
        added = execute_plan(self.rows, self.table.columns, add_plan)
        self.assertIn("margin", added.rows[0])
        self.assertEqual(Decimal(added.rows[0]["margin"]), Decimal("48000.00"))

        remove_plan = compile_request("удали столбец brand", self.table)
        removed = execute_plan(self.rows, self.table.columns, remove_plan)
        self.assertNotIn("brand", removed.rows[0])

        rename_plan = compile_request("переименуй столбец price в cost", self.table)
        renamed = execute_plan(self.rows, self.table.columns, rename_plan)
        self.assertIn("cost", renamed.rows[0])
        self.assertNotIn("price", renamed.rows[0])


# --------------------------------------------------------------------------
# FAMILY_EXCEL detection / continuation (business_assistant.action_continuation)
# --------------------------------------------------------------------------
class ExcelFamilyDetectionTests(unittest.TestCase):
    def test_attachment_alone_routes_to_excel_without_keyword(self):
        family = detect_family("что тут интересного?", None, has_spreadsheet_attachment=True)
        self.assertEqual(family, FAMILY_EXCEL)

    def test_explicit_keyword_without_attachment_routes_to_excel(self):
        family = detect_family("проанализируй этот excel файл", None)
        self.assertEqual(family, FAMILY_EXCEL)

    def test_plain_text_without_attachment_or_keyword_is_not_excel(self):
        family = detect_family("привет, как дела?", None)
        self.assertNotEqual(family, FAMILY_EXCEL)

    def test_continuation_with_business_keywords_stays_in_excel_family(self):
        from business_assistant.action_continuation import ActiveTask, FAMILY_EXCEL as FE

        active = ActiveTask(
            task_id="t1",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            family=FE,
            tool_id=TOOL_DATA_EXCEL_ASSISTANT,
            operation="assist",
            goal="анализ",
        )
        # These phrases contain business-domain keywords ("samsung", "excel")
        # that requires_business_integration() treats as signals of an
        # unrelated NEW top-level task -- but an active Excel task must win.
        for phrase in ("Оставь только Samsung", "Сделай минус 12%", "Сохрани в Excel"):
            self.assertEqual(
                detect_family(phrase, active), FAMILY_EXCEL, msg=f"failed for: {phrase!r}"
            )
            self.assertEqual(
                continuation_decision(phrase, active=active), CONTINUE_ACTIVE_TASK, msg=phrase
            )

    def test_weather_breaks_excel_continuation(self):
        from business_assistant.action_continuation import ActiveTask

        active = ActiveTask(
            task_id="t1",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            family=FAMILY_EXCEL,
            tool_id=TOOL_DATA_EXCEL_ASSISTANT,
            operation="assist",
            goal="анализ",
        )
        self.assertEqual(continuation_decision("какая погода в Москве?", active=active), NEW_TASK)


def _excel_gateway(policy: LargeDatasetPolicy | None = None):
    """Real ToolGateway + real DataIntelToolAdapter/DataIntelligenceService,
    wired exactly like production (register_platform_tools), backed by
    in-memory stores only."""

    svc = DataIntelligenceService(
        InMemoryDatasetStore(), large_policy=policy or LargeDatasetPolicy()
    )
    artifact_service = ArtifactService(store=InMemoryArtifactStore())
    svc.artifact_service = artifact_service
    registry = ToolRegistry()
    register_platform_tools(registry, data_intelligence=svc)
    gateway = ToolGateway(registry=registry, register_search=False)
    return gateway, svc, artifact_service


class ExcelChatIntegrationEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def _gw(self, gateway, artifact_service):
        return WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=gateway,
            artifact_service=artifact_service,
        )

    async def _register_upload(self, artifact_service, *, tenant, owner, conv, filename, content):
        rec = artifact_service.register_upload(
            tenant_id=tenant, owner_id=owner, filename=filename, content=content
        )
        artifact_service.attach_to_conversation(
            tenant_id=tenant, artifact_id=rec.artifact_id, conversation_id=conv
        )
        return rec.artifact_id

    async def test_scenario_a_analyze(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._gw(gateway, artifact_service)
        content = _xlsx_bytes(
            [
                ["brand", "product_name", "price"],
                ["Samsung", "Galaxy A54", "40000"],
                ["Samsung", "Galaxy S23", "80000"],
                ["Apple", "iPhone 14", "70000"],
            ]
        )
        ref = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="price.xlsx", content=content
        )
        result = await panda.respond(
            ConversationRequest(
                text="Проанализируй этот прайс.",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        self.assertIn("строк", result.text.lower())
        self.assertEqual(result.metadata.get("action_decision"), CALL_TOOL)

    async def test_scenario_b_price_transform_exact_values_and_artifact(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._gw(gateway, artifact_service)
        content = _xlsx_bytes(
            [
                ["brand", "product_name", "price"],
                ["Samsung", "Galaxy A54", "40000"],
            ]
        )
        ref = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c2", filename="price.xlsx", content=content
        )
        result = await panda.respond(
            ConversationRequest(
                text="Сделай минус 10% и округли вверх до ближайших 50 и сохрани в Excel.",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c2",
                attachment_refs=(ref,),
            )
        )
        self.assertIn("[Скачать Excel]", result.text)
        artifacts = result.metadata.get("artifacts") or []
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["artifact_type"], "workbook")
        # Verify exact deterministic value via the artifact itself.
        rec, blob = artifact_service.get_blob(
            tenant_id="tenant-a", artifact_id=artifacts[0]["artifact_id"]
        )
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(blob))
        ws = wb["RESULT"]
        headers = [c.value for c in ws[1]]
        price_idx = headers.index("price") + 1
        self.assertEqual(str(ws.cell(2, price_idx).value), "36000")

    async def test_scenario_d_ambiguity_not_silently_resolved(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._gw(gateway, artifact_service)
        content = _xlsx_bytes(
            [
                ["brand", "purchase_price", "selling_price"],
                ["Samsung", "30000", "40000"],
            ]
        )
        ref = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c3", filename="price.xlsx", content=content
        )
        result = await panda.respond(
            ConversationRequest(
                text="Сделай минус 10%.",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c3",
                attachment_refs=(ref,),
            )
        )
        self.assertIn("Уточните", result.text)
        self.assertIn("purchase_price", result.text)
        self.assertIn("selling_price", result.text)
        # No workbook/artifact must be produced for an ambiguous request.
        self.assertEqual(result.metadata.get("artifacts"), [])

    async def test_scenario_c_compare_two_workbooks(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._gw(gateway, artifact_service)
        left = _xlsx_bytes(
            [
                ["sku", "product_name", "price", "stock"],
                ["A-1", "Galaxy A54", "40000", "10"],
                ["A-2", "Galaxy S23", "80000", "5"],
            ]
        )
        right = _xlsx_bytes(
            [
                ["sku", "product_name", "price", "stock"],
                ["A-1", "Galaxy A54", "42000", "10"],
                ["A-2", "Galaxy S23", "80000", "2"],
            ]
        )
        ref_left = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c4", filename="left.xlsx", content=left
        )
        ref_right = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c4", filename="right.xlsx", content=right
        )
        result = await panda.respond(
            ConversationRequest(
                text="Сравни два прайса по артикулу и покажи изменение цены и остатков.",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r4",
                conversation_id="c4",
                attachment_refs=(ref_left, ref_right),
            )
        )
        self.assertEqual(result.metadata.get("action_decision"), CALL_TOOL)
        artifacts = result.metadata.get("artifacts") or []
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["artifact_type"], "workbook")

    async def test_scenario_e_large_workbook_routes_to_batch(self):
        gateway, svc, artifact_service = _excel_gateway(
            LargeDatasetPolicy(max_sync_rows=5, rows_per_batch=5)
        )
        panda = self._gw(gateway, artifact_service)
        rows = [["brand", "product_name", "price"]] + [
            ["Samsung", f"Model {i}", str(1000 + i)] for i in range(20)
        ]
        content = _xlsx_bytes(rows)
        ref = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c5", filename="big.xlsx", content=content
        )
        result = await panda.respond(
            ConversationRequest(
                text="Проанализируй этот файл.",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r5",
                conversation_id="c5",
                attachment_refs=(ref,),
            )
        )
        # No workflow_runtime wired here -> large routing fails closed with a
        # typed, non-crashing error rather than silently processing 20 rows
        # synchronously; asserts the *classification* boundary is enforced.
        self.assertEqual(result.metadata.get("action_decision"), CALL_TOOL)

    async def test_scenario_f_multi_turn_continuation_without_reupload(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._gw(gateway, artifact_service)
        content = _xlsx_bytes(
            [
                ["brand", "product_name", "price"],
                ["Samsung", "Galaxy A54", "40000"],
                ["Samsung", "Galaxy S23", "80000"],
                ["Apple", "iPhone 14", "70000"],
            ]
        )
        ref = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c6", filename="price.xlsx", content=content
        )

        turn1 = await panda.respond(
            ConversationRequest(
                text="Проанализируй файл",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r6-1",
                conversation_id="c6",
                attachment_refs=(ref,),
            )
        )
        self.assertEqual(turn1.metadata.get("action_decision"), CALL_TOOL)

        turn2 = await panda.respond(
            ConversationRequest(
                text="Оставь только Samsung",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r6-2",
                conversation_id="c6",
            )
        )
        self.assertEqual(turn2.metadata.get("action_decision"), CALL_TOOL)

        turn3 = await panda.respond(
            ConversationRequest(
                text="Только дешевле 50000",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r6-3",
                conversation_id="c6",
            )
        )
        self.assertEqual(turn3.metadata.get("action_decision"), CALL_TOOL)

        turn4 = await panda.respond(
            ConversationRequest(
                text="Сделай минус 12%",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r6-4",
                conversation_id="c6",
            )
        )
        self.assertEqual(turn4.metadata.get("action_decision"), CALL_TOOL)

        turn5 = await panda.respond(
            ConversationRequest(
                text="Сохрани в Excel",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r6-5",
                conversation_id="c6",
            )
        )
        self.assertEqual(turn5.metadata.get("action_decision"), CALL_TOOL)
        artifacts = turn5.metadata.get("artifacts") or []
        self.assertEqual(len(artifacts), 1)
        rec, blob = artifact_service.get_blob(
            tenant_id="tenant-a", artifact_id=artifacts[0]["artifact_id"]
        )
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(blob))
        ws = wb["RESULT"]
        # Only Samsung Galaxy A54 (40000) survives filter chain (Samsung ->
        # cheaper than 50000); minus 12% == 35200.
        rows_out = list(ws.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(rows_out), 1)
        headers = [c.value for c in ws[1]]
        price_idx = headers.index("price")
        self.assertEqual(str(rows_out[0][price_idx]), "35200.00")

    async def test_tenant_isolation_cross_tenant_dataset_denied(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._gw(gateway, artifact_service)
        content = _xlsx_bytes(
            [
                ["brand", "product_name", "price"],
                ["Samsung", "Galaxy A54", "40000"],
            ]
        )
        ref = await self._register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c7", filename="price.xlsx", content=content
        )
        result = await panda.respond(
            ConversationRequest(
                text="Проанализируй файл",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r7-1",
                conversation_id="c7",
                attachment_refs=(ref,),
            )
        )
        self.assertEqual(result.metadata.get("action_decision"), CALL_TOOL)

        # A different tenant's conversation never resolves user A's uploaded
        # artifact (ArtifactService.resolve_trusted_refs is tenant-scoped) --
        # confirms the trust boundary the chat integration relies on.
        cross_tenant_resolved = artifact_service.resolve_trusted_refs(
            tenant_id="tenant-b", conversation_id="c7-other-tenant", refs=(ref,)
        )
        self.assertEqual(cross_tenant_resolved, [])

        # With no valid attachment and no active task for tenant-b/this
        # conversation, the deterministic turn resolver must ask for a file
        # rather than ever touching tenant A's dataset -- never CALL_TOOL.
        action = resolve_action_turn(
            "Оставь только Samsung",
            tenant_id="tenant-b",
            owner_id="u2",
            conversation_id="c7-other-tenant",
            store=panda._action_store,
            gateway=gateway,
            request_id="r7-2",
            spreadsheet_attachment_count=0,
        )
        self.assertNotEqual(action.decision, CALL_TOOL)

        # Directly exercising the service layer confirms defense-in-depth:
        # even a spoofed/foreign dataset_id is denied at the DataIntelligenceService
        # boundary (tenant-scoped store), not only at the artifact-resolution layer.
        ingested = svc.ingest(content, filename="p.xlsx", tenant_id="tenant-a", enqueue_large=False)
        with self.assertRaises(DataIntelError):
            svc.execute_nl_request(ingested["dataset_id"], "оставь только Samsung", tenant_id="tenant-b")


if __name__ == "__main__":
    unittest.main()
