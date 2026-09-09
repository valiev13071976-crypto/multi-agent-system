"""PANDA — PRODUCTION DEFECT CLOSURE: uploaded XLSX product context lost
between the upload turn and a later enrichment turn naming the SKU.

Reproduced production defect (SAME conversation, no re-upload):

    Turn 1: user uploads "LG_TV.xlsx" with a generic message (no specific
            SKU/row named). Panda replies with a generic analyze-only
            summary, e.g. "В таблице N строк и M столбцов...". No
            ``ROW_FOUND`` happened, so ``bitrix_product_fields`` was NEVER
            persisted on the ``ActiveTask`` -- only ``dataset_id`` was.
    Turn 2: "Подготовь полную карточку товара LG 55MRGB86B6A.ARUG из
            загруженного прайса..." -- names the SKU for the first time.

ACTUAL (before fix): ``resolve_product_enrichment_request`` (business_
assistant/action_continuation.py) only ever reads pre-persisted
``bitrix_product_fields``; finding none, it immediately gives up with
"Не вижу товара для подготовки полной карточки...", even though the
exact same SKU is sitting right there in the already-parsed dataset.

FIX: when ``bitrix_product_fields`` is missing but a ``dataset_id`` from
the upload turn is present, ``resolve_product_enrichment_request`` now
returns a CALL_TOOL fallback that re-runs the EXISTING, unchanged
``data.excel_assistant``/``assist`` row lookup against that dataset with
THIS turn's text (which names the SKU) -- exactly the same lookup that
would have produced ``ROW_FOUND`` had the user named the SKU on turn 1.
``WorkflowPandaConversationGateway.respond()`` (business_assistant/
conversation_gateway.py) chains straight into ``CALL_PRODUCT_ENRICHMENT``
in the SAME turn once/if that lookup resolves a single row, via the new
``ActionDecision.chain_to_enrichment_text`` marker and
``_maybe_chain_to_enrichment()`` helper. Neither the enrichment pipeline
itself, Brave/Search wiring, nor the Bitrix write path are touched.

Uses the FIXTURE Bitrix adapter and the default ``ToolGateway`` with
``register_search=False`` (``NullSearchProvider``, zero network) --
mirrors ``tests/test_panda_product_enrichment_conversational.py``. Zero
live Bitrix mutations."""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CALL_PRODUCT_ENRICHMENT
from business_assistant.conversation_gateway import ConversationRequest, WorkflowPandaConversationGateway
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

# Turn 1: plain upload, generic message -- deliberately does NOT name any
# SKU/row, so the excel tool can only produce an ANALYZED (not ROW_FOUND)
# summary and bitrix_product_fields is never persisted.
GENERIC_UPLOAD_TURN_TEXT = "Проанализируй загруженный прайс."

# Turn 2: the exact reproduced production request -- names the SKU for the
# first time, on the enrichment turn itself, with NO new attachment.
ENRICHMENT_TURN_WITH_SKU_TEXT = (
    f"Подготовь полную карточку товара LG {TARGET_SKU} из загруженного прайса для Bitrix/Aspro. "
    f"Используй EAN {TARGET_EAN}. Ничего в Bitrix не записывай. Покажи полный предпросмотр и источники."
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


def _panda(*, bitrix_bridge=None):
    svc = DataIntelligenceService(InMemoryDatasetStore())
    artifact_service = ArtifactService(store=InMemoryArtifactStore())
    svc.artifact_service = artifact_service
    registry = ToolRegistry()
    register_platform_tools(registry, data_intelligence=svc)
    gateway = ToolGateway(registry=registry, register_search=False)
    panda = WorkflowPandaConversationGateway(
        workflow_engine=object(),
        run_router=object(),
        context_manager=object(),
        tool_gateway=gateway,
        artifact_service=artifact_service,
        bitrix_product_bridge=bitrix_bridge,
    )
    return panda, artifact_service


class EnrichmentDatasetContextRestorationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_upload_then_named_sku_enrichment_restores_context(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        rec = artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="u1", filename="LG_TV.xlsx", content=_xlsx_bytes()
        )
        artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="c1"
        )

        # Turn 1: plain upload, no SKU named -> generic analyze-only reply.
        turn1 = await panda.respond(
            ConversationRequest(
                text=GENERIC_UPLOAD_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(rec.artifact_id,),
            )
        )
        self.assertNotEqual(turn1.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        self.assertIn("столбц", turn1.text.casefold())
        before = len(store.catalog("tenant-a"))

        # Turn 2 (SAME conversation, NO new attachment): names the SKU for
        # the first time -- must restore the previously parsed product
        # context and invoke enrichment, WITHOUT asking the user to
        # re-upload the file.
        turn2 = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_WITH_SKU_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )

        self.assertEqual(turn2.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        self.assertNotIn("Не вижу товара", turn2.text)
        self.assertIn("READY TO WRITE", turn2.text)
        self.assertIn("НЕ подтверждение записи", turn2.text)
        preview = turn2.metadata.get("enrichment_preview") or {}
        self.assertEqual(preview.get("identity", {}).get("article"), TARGET_SKU)
        self.assertEqual(preview.get("identity", {}).get("ean"), TARGET_EAN)

        # ZERO Bitrix mutation from enrichment alone.
        self.assertEqual(len(store.catalog("tenant-a")), before)


if __name__ == "__main__":
    unittest.main()
