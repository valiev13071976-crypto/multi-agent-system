"""PANDA — PRODUCTION DEFECT CLOSURE: XLSX attachment and the enrichment
instruction submitted in the SAME message (one-turn dispatch).

Reproduced production failure (HTTP level):

    POST /attachments            -> artifact://upload/<uuid>/LG_TV.xlsx
    POST /requests               -> message = "Подготовь полную карточку
                                    товара LG 55MRGB86B6A.ARUG из
                                    загруженного прайса ..." WITH that
                                    artifact_ref on the SAME request
    -> "Не вижу товара для подготовки полной карточки..."

Root cause: ``business_assistant.action_continuation.resolve_action_turn``
checks ``is_explicit_product_enrichment_request`` unconditionally, up
front, before any family/attachment handling, and
``resolve_product_enrichment_request`` returns
``_enrichment_missing_context_decision`` on its first guard
(``active is None or active.family != FAMILY_EXCEL``). On a one-turn
request there is no ``ActiveTask`` yet, so the request was rejected
without the attached workbook ever being parsed -- the XLSX is only
ingested later, inside ``data_intel.tools.DataIntelToolAdapter._assist``,
which is reachable exclusively through a CALL_TOOL decision.

PR #50 fixed generic-workflow misrouting; PR #51 fixed the two-turn case
where a ``dataset_id`` already existed. Neither covers this earlier
lifecycle stage (nothing ingested yet, because the file arrived on this
very turn), because #51's fallback sits AFTER the ``active is None``
guard.

Fix: ``_enrichment_needs_excel_ingestion_first`` + the dispatch branch in
``resolve_action_turn`` route such a turn through the EXISTING
FAMILY_EXCEL ingestion/row-lookup path first (exactly what a "Найди товар
<SKU>..." turn does), then reuse #51's ``chain_to_enrichment_text`` /
``WorkflowPandaConversationGateway._maybe_chain_to_enrichment`` so the
same turn continues into CALL_PRODUCT_ENRICHMENT once the row resolves.

This test drives the REAL ``BusinessAssistantApiService`` (the object the
HTTP handlers use) with the REAL ``upload_attachment`` ref format, a
fresh conversation, and NO pre-seeded ActiveTask/dataset_id/
bitrix_product_fields -- which is exactly what distinguishes it from
#51's two-turn test. Fixture Bitrix adapter only; zero live mutations."""

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
from business_assistant_api.models import ST_COMPLETED, WORKLOAD_INTERACTIVE
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
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The exact reproduced production message -- submitted TOGETHER with the
# LG_TV.xlsx attachment in a single POST /requests.
PRODUCTION_ONE_TURN_TEXT = (
    f"Подготовь полную карточку товара LG {TARGET_SKU} из загруженного прайса для Bitrix/Aspro. "
    f"Используй EAN {TARGET_EAN}. Выполни реальный поиск в интернете через доступный Search, "
    "собери подтверждённые характеристики, описание и подходящие изображения. "
    "Ничего в Bitrix не записывай. Покажи полный предпросмотр и источники."
)


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
    activation._adapters["bitrix"] = BitrixFixtureAdapter(store=store)  # noqa: SLF001
    ref = activation.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-tenant-a", value="tok")
    conn = activation.configure_connection(
        tenant_id="tenant-a", provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
    )
    activation.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    activation.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, store


class OneTurnAttachmentEnrichmentHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bridge, self.catalog = _bitrix_bridge()
        data_intel = DataIntelligenceService(InMemoryDatasetStore())
        self.artifact_service = ArtifactService(store=InMemoryArtifactStore())
        data_intel.artifact_service = self.artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=data_intel)
        gateway = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=ToolGateway(registry=registry, register_search=False),
            artifact_service=self.artifact_service,
            bitrix_product_bridge=self.bridge,
        )
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "ba_one_turn.sqlite"),
            conversation_gateway=gateway,
            artifact_service=self.artifact_service,
        )
        self.svc = self.rt.service

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_attachment_and_enrichment_text_in_one_request_reaches_preview(self):
        # POST /attachments -- the real upload path and ref format.
        upload = self.svc.upload_attachment(
            tenant_id="tenant-a",
            owner_id="user-a",
            filename="LG_TV.xlsx",
            content=_xlsx_bytes(),
            mime_type=XLSX_MIME,
            upload_base_dir=os.path.join(self.tmp, "uploads"),
        )
        self.assertTrue(upload["artifact_ref"].startswith("artifact://upload/"))
        self.assertEqual(upload["kind"], "spreadsheet")
        before = len(self.catalog.catalog("tenant-a"))

        # POST /requests -- ONE submit, fresh conversation, attachment AND
        # enrichment text together. Nothing is pre-seeded: no ActiveTask,
        # no dataset_id, no bitrix_product_fields.
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=PRODUCTION_ONE_TURN_TEXT,
            artifact_refs=[upload["artifact_ref"]],
            conversation_id="conv-one-turn",
            idempotency_key="one-turn-enrichment-1",
        )

        self.assertEqual(rec.workload_class, WORKLOAD_INTERACTIVE)
        self.assertEqual(rec.status, ST_COMPLETED)

        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id)
        summary = result["summary"]
        self.assertNotIn("Не вижу товара", summary)
        self.assertIn("READY TO WRITE", summary)
        self.assertIn("НЕ подтверждение записи", summary)
        self.assertIn(TARGET_SKU, summary)
        self.assertIn(TARGET_EAN, summary)

        # ZERO Bitrix mutation from enrichment alone.
        self.assertEqual(len(self.catalog.catalog("tenant-a")), before)


if __name__ == "__main__":
    unittest.main()
