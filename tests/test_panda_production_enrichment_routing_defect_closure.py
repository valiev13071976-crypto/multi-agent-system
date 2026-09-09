"""PANDA — PRODUCTION DEFECT CLOSURE: complete-product-card request routed to
the degraded generic Business Workflow instead of Product Enrichment.

Reproduced production defect:

    Turn 1: user uploads a supplier XLSX and asks Panda to find/prepare a
            specific LG TV for Bitrix/Aspro (attachment present -> already
            correctly routed to the conversational Panda AI core, ROW_FOUND
            preview persists ``bitrix_product_fields`` on the ActiveTask).
    Turn 2 (SAME conversation, no new attachment): "Подготовь полную
            карточку товара LG 55MRGB86B6A.ARUG из загруженного прайса для
            Bitrix/Aspro. ... Ничего в Bitrix не записывай. Покажи полный
            предпросмотр и источники."

Root cause: this exact wording contains a domain term ("Bitrix"/"Aspro")
and an action verb ("Подготовь"), so
``business_assistant.intent.requires_business_integration`` returned True
for it whenever the turn itself carried no ``artifact_refs`` (the file was
uploaded on an EARLIER turn -- "из загруженного прайса" -- not re-attached
to this one). ``classify_intent`` then routed the request into the fixture
``BusinessAssistantService.execute()`` 12-step ``SUPPLIER_PRICE`` recipe
instead of ``WorkflowPandaConversationGateway``'s
``CALL_PRODUCT_ENRICHMENT`` path -- that recipe immediately blocks on a
Bitrix-integration capability it never needed (product_enrichment never
writes to Bitrix by itself), producing exactly the reported
``BA_CAPABILITY_UNAVAILABLE``/``dependency_not_ready``-degraded
``COMPLETED_WITH_WARNINGS`` result with 10 blocked steps, which the
frontend then renders as the generic "Задача выполнена. Подробности
доступны в разделе управления." fallback.

Fix: ``business_assistant.intent.is_conversational`` now recognizes an
explicit product-card enrichment request (reusing the EXISTING
``business_assistant.action_continuation.is_explicit_product_enrichment_
request`` detector -- no new pattern, no duplicated logic) and routes it
to the conversational pipeline regardless of domain terms/attachments,
since enrichment never mutates Bitrix by itself (a separate, still
unchanged, explicit confirmation is required for any actual write).

This test exercises the REAL production stack end-to-end through
``BusinessAssistantApiService`` (the same object the HTTP API's
``POST /api/v1/business-assistant/requests`` handler uses) with a REAL
``WorkflowPandaConversationGateway`` (not the ``FakePandaConversationGateway``
double used by other routing tests) wired to a fixture Bitrix bridge, so a
regression here would be caught even if the intent/routing layer alone
were tested in isolation."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant_api.models import ST_COMPLETED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter
from integrations.bitrix.product_bridge import BitrixProductBridge
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

TARGET_SKU = "55MRGB86B6A.ARUG"
TARGET_EAN = "8806096824788"

PREVIEW_TURN_TEXT = (
    f"Найди товар LG {TARGET_SKU} и подготовь его для добавления в Bitrix/Aspro Premier. "
    "Розничная цена 139990 \u20bd."
)
# The EXACT reproduced production request (turn 2, no artifact_refs on
# this specific message -- the price list was uploaded on turn 1).
PRODUCTION_ENRICHMENT_TEXT = (
    f"Подготовь полную карточку товара LG {TARGET_SKU} из загруженного прайса для Bitrix/Aspro. "
    f"Используй EAN {TARGET_EAN}. Выполни реальный поиск в интернете через доступный Search, "
    "собери подтверждённые характеристики, описание и подходящие изображения. "
    "Ничего в Bitrix не записывай. Покажи полный предпросмотр и источники."
)

# Markers that only ever appear in the LEGACY fixture business-workflow's
# diagnostic summary (business_assistant.service.BusinessAssistantService.
# _compose_summary) -- never in a conversational ConversationResult.text.
# Their presence would mean this request degraded to the generic workflow.
WORKFLOW_DIAGNOSTIC_MARKERS = ("Requested:", "Findings:", "Fixture_mode:", "Waiting_approval:")


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price"])
    ws.append([TARGET_SKU, f"LG {TARGET_SKU}", "TV", "LG", TARGET_EAN, "103198.3"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _bitrix_bridge() -> tuple[BitrixProductBridge, BitrixCatalogStore]:
    store = BitrixCatalogStore()
    activation = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    activation._adapters["bitrix"] = adapter  # noqa: SLF001
    ref = activation.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-tenant-a", value="tok")
    conn = activation.configure_connection(
        tenant_id="tenant-a", provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
    )
    activation.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    activation.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, store


class ProductionCompleteCardRoutingDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT production scenario end-to-end via the same
    ``BusinessAssistantApiService`` the HTTP API uses, with a REAL
    ``WorkflowPandaConversationGateway`` (never the fake double)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_prod_enrich_defect.sqlite")
        self.bridge, self.store = _bitrix_bridge()

        svc = DataIntelligenceService(InMemoryDatasetStore())
        self.artifact_service = ArtifactService(store=InMemoryArtifactStore())
        svc.artifact_service = self.artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=svc)
        # No configured search backend needed to prove the ROUTING fix --
        # research fails safe (empty facts) exactly like requirement 15
        # dictates; the preview must still be produced and returned.
        gateway = ToolGateway(registry=registry, register_search=False)
        self.conversation_gateway = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=gateway,
            artifact_service=self.artifact_service,
            bitrix_product_bridge=self.bridge,
        )
        self.rt = build_business_assistant_api_runtime(
            db_path=self.db,
            conversation_gateway=self.conversation_gateway,
            artifact_service=self.artifact_service,
        )
        self.svc = self.rt.service

    async def asyncTearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_production_enrichment_request_reaches_preview_not_degraded_workflow(self):
        # Turn 1: XLSX attachment + Bitrix-flavored preview request.
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename="LG.xlsx", content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-1"
        )
        turn1 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=PREVIEW_TURN_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-1",
            idempotency_key="prod-defect-turn1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)
        before_catalog_size = len(self.store.catalog("tenant-a"))

        # Turn 2: the EXACT reproduced production request -- SAME
        # conversation, NO artifact_refs on this message.
        turn2 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=PRODUCTION_ENRICHMENT_TEXT,
            conversation_id="conv-1",
            idempotency_key="prod-defect-turn2",
        )

        # The core defect: this must NOT degrade into the generic
        # blocked-capability business workflow.
        self.assertEqual(turn2.status, ST_COMPLETED)
        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary = result["summary"]
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary, f"turn2 degraded into the generic business workflow (found {marker!r})")

        # It must instead be the real Product Enrichment preview.
        self.assertIn("READY TO WRITE", summary)
        self.assertIn("НЕ подтверждение записи", summary)

        # ZERO Bitrix mutation from enrichment alone.
        self.assertEqual(len(self.store.catalog("tenant-a")), before_catalog_size)


if __name__ == "__main__":
    unittest.main()
