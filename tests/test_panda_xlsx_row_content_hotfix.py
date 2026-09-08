"""PANDA — PRODUCTION HOTFIX: XLSX content not used after successful parse.

PR #37 made the XLSX conversational path reachable (attachment-aware intent
routing + always-populated summaries), but the extracted table/row content
was still being reduced to bare dimension metadata ("В таблице 13 строк и 8
столбцов.") before it ever reached the tool's user-facing reply -- any
request that also named a specific product/SKU and asked Panda to act on it
(e.g. "find LG 32LQ63006LA.ARUG and prepare it for Bitrix/Aspro Premier")
got silently dropped by ``data_intel.nl_ops.compile_request`` (it only
understands bounded transform/filter/sort operations, not a "product ->
free-form data_intel/service.py`` still called ``_analyze_only_summary``
for anything it couldn't compile, discarding the real row content).

A second, related boundary: a bare continuation turn ("Продолжай и выполни
мой предыдущий запрос полностью.") carries no identifying content of its
own -- ``business_assistant/action_continuation.py``'s FAMILY_EXCEL branch
always sent the CURRENT turn's raw text to the tool, discarding the earlier
substantive instruction ``resolve_follow_up`` had already resolved from
conversation history.

Fixes pinned here (schema-driven, no hardcoded product/workbook/prompt):

1. ``data_intel/service.py``: when the NL compiler cannot compile a
   supported transform (``UnsupportedOperationError``), a generic,
   schema-driven single-row lookup (matching the table's own
   SKU/article/EAN/product-name column values against the free text) now
   runs before falling back to the dimension-only summary. On a match it
   returns a "ROW_FOUND" result with the row's real price/identifying
   values plus any user-supplied price parsed from the request text, and
   an explicit "not published/written" disclaimer.
2. ``business_assistant/action_continuation.py``: a FAMILY_EXCEL
   continuation turn whose own text needs prior context
   (``follow_up.inject_context``) now sends the tool the earlier resolved
   user instruction + the current turn's text, instead of losing the
   original request to a near-empty "continue" phrase.

No LLM, no new XLSX parser, no write/publish path is touched.
"""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CALL_TOOL
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from business_assistant.follow_up import HistoryTurn
from business_assistant_api.models import WORKLOAD_BATCH
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.contracts import (
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SKU,
    ColumnDescriptor,
    TableDescriptor,
)
from data_intel.service import DataIntelligenceService, _find_row_by_identifier
from data_intel.store import InMemoryDatasetStore
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

TARGET_SKU = "LG-32LQ63006LA-ARUG"
TARGET_PURCHASE_PRICE = "22513.70"
USER_RETAIL_PRICE_RUB = "29990"

REPRO_TEXT = (
    "Проанализируй загруженный прайс. Цены в файле — закупочные. "
    "Пока ничего не публикуй на сайт. "
    f"Найди товар {TARGET_SKU} и подготовь его для добавления в Bitrix/Aspro Premier. "
    f"Розничная цена {USER_RETAIL_PRICE_RUB} руб. "
    "Сначала покажи мне подготовленную карточку и план действий перед записью."
)

FOLLOW_UP_TEXT = "Продолжай и выполни мой предыдущий запрос полностью."


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _price_list_bytes() -> bytes:
    return _xlsx_bytes(
        [
            ["sku", "product_name", "purchase_price"],
            ["SAM-A54", "Galaxy A54", "18000.00"],
            [TARGET_SKU, "LG 32LQ63006LA.ARUG TV", TARGET_PURCHASE_PRICE],
            ["APL-14", "iPhone 14", "70000.00"],
        ]
    )


def _table(sku_col="sku", price_col="purchase_price", name_col="product_name") -> TableDescriptor:
    columns = (
        ColumnDescriptor(source_name=sku_col, normalized_name=sku_col, semantic_role=ROLE_SKU),
        ColumnDescriptor(source_name=name_col, normalized_name=name_col, semantic_role=ROLE_PRODUCT_NAME),
        ColumnDescriptor(source_name=price_col, normalized_name=price_col, semantic_role=ROLE_PURCHASE_PRICE),
    )
    return TableDescriptor(table_id="t1", sheet="Sheet1", range="A1", header_row=1, columns=columns, row_count=3)


class RowLookupUnitTests(unittest.TestCase):
    """Direct unit coverage for the schema-driven row-matching helper."""

    def _rows(self):
        return [
            {"sku": "SAM-A54", "product_name": "Galaxy A54", "purchase_price": "18000.00"},
            {"sku": TARGET_SKU, "product_name": "LG 32LQ63006LA.ARUG TV", "purchase_price": TARGET_PURCHASE_PRICE},
            {"sku": "APL-14", "product_name": "iPhone 14", "purchase_price": "70000.00"},
        ]

    def test_unique_identifier_match_found(self):
        hit = _find_row_by_identifier(f"Найди товар {TARGET_SKU} и подготовь его", self._rows(), _table())
        self.assertIsNotNone(hit)
        row, column, value = hit
        self.assertEqual(value, TARGET_SKU)
        self.assertEqual(row["purchase_price"], TARGET_PURCHASE_PRICE)

    def test_no_match_returns_none(self):
        hit = _find_row_by_identifier("Проанализируй прайс, ничего конкретного", self._rows(), _table())
        self.assertIsNone(hit)

    def test_ambiguous_multi_match_returns_none(self):
        # A short, low-signal value present in more than one row must not be
        # guessed at -- large-batch/regular analyze/transform behaviour must
        # stay completely unaffected.
        rows = [
            {"sku": "A-1", "product_name": "Widget", "purchase_price": "10"},
            {"sku": "A-12", "product_name": "Widget Pro", "purchase_price": "20"},
        ]
        hit = _find_row_by_identifier("Найди товар A-1 и подготовь", rows, _table())
        self.assertIsNone(hit)


def _excel_gateway():
    svc = DataIntelligenceService(InMemoryDatasetStore())
    artifact_service = ArtifactService(store=InMemoryArtifactStore())
    svc.artifact_service = artifact_service
    registry = ToolRegistry()
    register_platform_tools(registry, data_intelligence=svc)
    gateway = ToolGateway(registry=registry, register_search=False)
    return gateway, svc, artifact_service


class XlsxRowContentEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Full path through the real conversational gateway + real data_intel
    tool -- no fakes for the parsing/compilation logic under test."""

    def _panda(self, gateway, artifact_service):
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

    async def test_row_data_and_user_instruction_reach_the_reply(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._panda(gateway, artifact_service)
        ref = await self._register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )
        result = await panda.respond(
            ConversationRequest(
                text=REPRO_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        # (1) content reaches execution, not only dimensions/metadata.
        self.assertNotIn("столбцов.", result.text)
        # (2) the specific row/value is located.
        self.assertIn(TARGET_SKU, result.text)
        self.assertIn(TARGET_PURCHASE_PRICE, result.text)
        # (3) the user-supplied instruction content (retail price) survives
        # alongside the extracted XLSX content.
        self.assertIn(USER_RETAIL_PRICE_RUB, result.text)
        # (4) preview/no-write: nothing was published/written.
        self.assertIn("не выполнена", result.text)
        self.assertEqual(result.metadata.get("action_decision"), CALL_TOOL)
        self.assertEqual(result.metadata.get("artifacts"), [])

    async def test_followup_continue_reuses_prior_instruction_without_reupload(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._panda(gateway, artifact_service)
        ref = await self._register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c2",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )
        first = await panda.respond(
            ConversationRequest(
                text=REPRO_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c2",
                attachment_refs=(ref,),
            )
        )
        self.assertIn(TARGET_SKU, first.text)

        history = (
            HistoryTurn(role="user", content=REPRO_TEXT),
            HistoryTurn(role="assistant", content=first.text),
        )
        second = await panda.respond(
            ConversationRequest(
                text=FOLLOW_UP_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c2",
                history=history,
                # No re-upload -- the previously attached workbook must be
                # reused via the already-persisted dataset_id.
                attachment_refs=(),
            )
        )
        self.assertIn(TARGET_SKU, second.text)
        self.assertIn(TARGET_PURCHASE_PRICE, second.text)
        self.assertIn("не выполнена", second.text)
        self.assertEqual(second.metadata.get("action_decision"), CALL_TOOL)


class BatchRoutingUnaffectedTests(unittest.TestCase):
    """Existing large-XLSX batch routing (Block 5.1/5.2 scale-safe pipeline)
    must stay completely bypassed by this hotfix."""

    def test_large_batch_excel_still_routes_away_from_conversational_pipeline(self):
        import os
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp()
        try:
            rt = build_business_assistant_api_runtime(db_path=os.path.join(tmp, "ba.sqlite"))
            rec = rt.service.submit(
                tenant_id="tenant-a",
                owner_id="user-a",
                message="Сравни закупку с текущими ценами и подготовь итоговую Excel таблицу.",
                artifact_refs=["artifact://excel/price-list.xlsx"],
                idempotency_key="hotfix-batch-1",
            )
            self.assertEqual(rec.workload_class, WORKLOAD_BATCH)
        finally:
            rt.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
