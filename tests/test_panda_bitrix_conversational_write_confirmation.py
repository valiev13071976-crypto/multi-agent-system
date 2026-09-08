"""PANDA — first controlled production Bitrix product write: conversational
approval glue (PR #43, follow-up).

Covers the minimal conversational wiring that lets a user explicitly
approve creating a SPECIFIC, already-previewed product in Bitrix, e.g.:

    «Подтверждаю: создай этот товар в Bitrix. Розничная цена 29990 ₽.»

after Panda has already prepared an XLSX product preview
(``data_intel.service.DataIntelligenceService._row_lookup_result``,
status ``ROW_FOUND``). The approval routes through
``business_assistant.action_continuation.resolve_bitrix_write_confirmation``
straight to the pre-existing, unchanged
``business_assistant.controlled_bitrix_write.execute_single_product_write``
-- no new write path, no bypass of PR #43's approval/idempotency/read-back
gate.

Cursor performs ZERO real production mutations while implementing/testing
this: every test below runs exclusively against the deterministic FIXTURE
Bitrix adapter/store, never a live connection.
"""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CALL_CONTROLLED_BITRIX_WRITE, CALL_TOOL
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
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

TARGET_SKU = "32LQ63006LA.ARUG"
TARGET_PRODUCT_NAME = "LG 32LQ63006LA.ARUG TV"
TARGET_CATEGORY = "Televisions"
TARGET_BRAND = "LG"
TARGET_PURCHASE_PRICE = "22513.70"
TARGET_STOCK = "7"
USER_RETAIL_PRICE_RUB = "29990"

PREVIEW_TURN_TEXT = (
    f"Найди товар LG {TARGET_SKU} и подготовь его для добавления в Bitrix/Aspro Premier. "
    f"Розничная цена {USER_RETAIL_PRICE_RUB} \u20bd."
)
EXPLICIT_APPROVAL_TEXT = (
    f"Подтверждаю: создай этот товар в Bitrix. Розничная цена {USER_RETAIL_PRICE_RUB} \u20bd."
)


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
            ["sku", "product_name", "category", "brand", "purchase_price", "stock"],
            ["SAM-A54", "Galaxy A54", "Phones", "Samsung", "18000.00", "12"],
            [
                TARGET_SKU,
                TARGET_PRODUCT_NAME,
                TARGET_CATEGORY,
                TARGET_BRAND,
                TARGET_PURCHASE_PRICE,
                TARGET_STOCK,
            ],
            ["APL-14", "iPhone 14", "Phones", "Apple", "70000.00", "3"],
        ]
    )


def _bitrix_bridge() -> tuple[BitrixProductBridge, BitrixCatalogStore]:
    store = BitrixCatalogStore()
    activation = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    activation._adapters["bitrix"] = adapter
    ref = activation.put_secret_ref(
        tenant_id="tenant-a", secret_ref="secret:bitrix-tenant-a", value="tok"
    )
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


async def _register_upload(artifact_service, *, tenant, owner, conv, filename, content):
    rec = artifact_service.register_upload(
        tenant_id=tenant, owner_id=owner, filename=filename, content=content
    )
    artifact_service.attach_to_conversation(
        tenant_id=tenant, artifact_id=rec.artifact_id, conversation_id=conv
    )
    return rec.artifact_id


class ExplicitApprovalInvokesControlledWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_confirmation_after_preview_creates_exactly_one_product(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )

        preview = await panda.respond(
            ConversationRequest(
                text=PREVIEW_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        self.assertEqual(preview.metadata.get("action_decision"), CALL_TOOL)
        before = len(store.catalog("tenant-a"))

        approval = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )

        self.assertEqual(approval.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        after = len(store.catalog("tenant-a"))
        self.assertEqual(after - before, 1)
        result = approval.metadata.get("bitrix_write_result") or {}
        self.assertTrue(result.get("mutated"))
        self.assertEqual(result.get("sku"), TARGET_SKU)


class PreviewDoesNotWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_alone_never_mutates_bitrix(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )

        before = len(store.catalog("tenant-a"))
        preview = await panda.respond(
            ConversationRequest(
                text=PREVIEW_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        self.assertEqual(preview.metadata.get("action_decision"), CALL_TOOL)
        self.assertNotEqual(preview.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(len(store.catalog("tenant-a")), before)


class VagueContinuationDoesNotWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_vague_phrases_never_invoke_controlled_write(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )
        await panda.respond(
            ConversationRequest(
                text=PREVIEW_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        before = len(store.catalog("tenant-a"))

        for vague_text in ("продолжай", "давай", "ок", "делай дальше"):
            result = await panda.respond(
                ConversationRequest(
                    text=vague_text,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id=f"r-{vague_text}",
                    conversation_id="c1",
                )
            )
            self.assertNotEqual(
                result.metadata.get("action_decision"),
                CALL_CONTROLLED_BITRIX_WRITE,
                msg=f"vague phrase {vague_text!r} must never trigger a Bitrix write",
            )

        self.assertEqual(len(store.catalog("tenant-a")), before)


class MissingProductContextDoesNotWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_without_prior_preview_asks_to_prepare_first(self):
        bridge, store = _bitrix_bridge()
        panda, _artifact_service = _panda(bitrix_bridge=bridge)
        before = len(store.catalog("tenant-a"))

        result = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c-no-preview",
            )
        )

        self.assertNotEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertIn("карточк", result.text.casefold())
        self.assertEqual(len(store.catalog("tenant-a")), before)

    async def test_confirmation_after_unrelated_active_task_asks_to_prepare_first(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        # A prior turn without any spreadsheet/preview context at all --
        # e.g. an image-generation task -- must not be treated as product
        # context either.
        await panda.respond(
            ConversationRequest(
                text="Сгенерируй логотип для кофейни",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c2",
            )
        )
        before = len(store.catalog("tenant-a"))

        result = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c2",
            )
        )

        self.assertNotEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(len(store.catalog("tenant-a")), before)


class ResultAndReadBackReturnedTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_result_and_read_back_verification_shown_to_user(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )
        await panda.respond(
            ConversationRequest(
                text=PREVIEW_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )

        approval = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )

        text = approval.text
        result = approval.metadata.get("bitrix_write_result") or {}
        self.assertTrue(result.get("mutated"))
        self.assertIn(str(result.get("bitrix_product_id")), text)
        self.assertIn(TARGET_SKU, text)
        self.assertIn(USER_RETAIL_PRICE_RUB, text)
        self.assertIn("подтверждена", text.casefold())
        read_back = result.get("read_back") or {}
        self.assertTrue(read_back.get("matches"))

    async def test_bitrix_unavailable_reports_capability_unavailable_and_does_not_mark_success(self):
        panda, artifact_service = _panda(bitrix_bridge=None)
        ref = await _register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )
        await panda.respond(
            ConversationRequest(
                text=PREVIEW_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )

        approval = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )
        self.assertIn("недоступна", approval.text.casefold())
        self.assertIsNone(approval.metadata.get("bitrix_write_result"))


if __name__ == "__main__":
    unittest.main()
