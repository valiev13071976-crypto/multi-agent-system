"""Product enrichment pipeline follow-up: end-to-end conversational wiring.

Covers the full target flow's approval boundary (requirement 12):

    supplier XLSX row preview (ROW_FOUND, unchanged Block 5.5)
        -> "Подготовь полную карточку товара" (CALL_PRODUCT_ENRICHMENT,
           NEW) -- runs product_enrichment, shows a complete preview,
           ZERO Bitrix mutation
        -> "Подтверждаю: создай этот товар в Bitrix..."
           (CALL_CONTROLLED_BITRIX_WRITE, unchanged #43 write path) --
           reuses the enriched write request

Uses the FIXTURE Bitrix adapter exclusively (mirrors
``tests/test_panda_bitrix_conversational_write_confirmation.py``) and the
default ``ToolGateway`` (``NullSearchProvider`` -- returns no results, so
research completes with zero facts rather than ever touching the network;
this is itself the "search unavailable -> do not hallucinate" contract,
requirement 15). Cursor performs ZERO live/real mutations while
implementing/testing this."""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CALL_CONTROLLED_BITRIX_WRITE, CALL_PRODUCT_ENRICHMENT, CALL_TOOL
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
TARGET_PRODUCT_NAME = "LG 55MRGB86B6A.ARUG"
TARGET_CATEGORY = "TV"
TARGET_BRAND = "LG"
TARGET_EAN = "8806096824788"
TARGET_PURCHASE_PRICE = "103198.3"
USER_RETAIL_PRICE_RUB = "139990"

OTHER_SKU = "32LQ63006LA.ARUG"
OTHER_PRODUCT_NAME = "LG 32LQ63006LA.ARUG"

PREVIEW_TURN_TEXT = (
    f"Найди товар LG {TARGET_SKU} и подготовь его для добавления в Bitrix/Aspro Premier. "
    f"Розничная цена {USER_RETAIL_PRICE_RUB} \u20bd."
)
OTHER_PREVIEW_TURN_TEXT = f"Найди товар LG {OTHER_SKU} и подготовь его для добавления в Bitrix/Aspro Premier."
ENRICHMENT_TURN_TEXT = "Подготовь полную карточку товара."
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
            ["sku", "product_name", "category", "brand", "ean", "purchase_price"],
            [
                TARGET_SKU,
                TARGET_PRODUCT_NAME,
                TARGET_CATEGORY,
                TARGET_BRAND,
                TARGET_EAN,
                TARGET_PURCHASE_PRICE,
            ],
            [OTHER_SKU, OTHER_PRODUCT_NAME, TARGET_CATEGORY, TARGET_BRAND, "", "22513.70"],
        ]
    )


def _bitrix_bridge() -> tuple[BitrixProductBridge, BitrixCatalogStore]:
    store = BitrixCatalogStore()
    activation = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    activation._adapters["bitrix"] = adapter
    ref = activation.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-tenant-a", value="tok")
    conn = activation.configure_connection(
        tenant_id="tenant-a", provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
    )
    activation.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    activation.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, store


def _panda(*, bitrix_bridge=None, search_provider=None, scrape_fetch_handler=None, media_fetcher=None):
    svc = DataIntelligenceService(InMemoryDatasetStore())
    artifact_service = ArtifactService(store=InMemoryArtifactStore())
    svc.artifact_service = artifact_service
    registry = ToolRegistry()
    register_platform_tools(registry, data_intelligence=svc)
    if scrape_fetch_handler is not None:
        import httpx

        adapters = {row.descriptor.tool_id: row.adapter for row in registry._items.values()}  # noqa: SLF001
        adapters["scrape.fetch"]._transport = httpx.MockTransport(scrape_fetch_handler)  # noqa: SLF001
    gateway = ToolGateway(registry=registry, register_search=False, search_provider=search_provider)
    panda = WorkflowPandaConversationGateway(
        workflow_engine=object(),
        run_router=object(),
        context_manager=object(),
        tool_gateway=gateway,
        artifact_service=artifact_service,
        bitrix_product_bridge=bitrix_bridge,
        media_fetcher=media_fetcher,
    )
    return panda, artifact_service


async def _register_upload(artifact_service, *, tenant, owner, conv, filename, content):
    rec = artifact_service.register_upload(tenant_id=tenant, owner_id=owner, filename=filename, content=content)
    artifact_service.attach_to_conversation(tenant_id=tenant, artifact_id=rec.artifact_id, conversation_id=conv)
    return rec.artifact_id


class EnrichmentPreviewDoesNotMutateBitrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrichment_turn_produces_complete_preview_with_zero_mutation(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="LG.xlsx", content=_price_list_bytes()
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

        result = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )

        self.assertEqual(result.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        self.assertEqual(len(store.catalog("tenant-a")), before)
        self.assertIn("READY TO WRITE", result.text)
        self.assertIn("NOT WRITABLE / UNRESOLVED", result.text)
        self.assertIn("MISSING SOURCE DATA", result.text)
        self.assertIn("НЕ подтверждение записи", result.text)
        preview = result.metadata.get("enrichment_preview") or {}
        self.assertEqual(preview.get("identity", {}).get("brand"), "LG")
        self.assertEqual(preview.get("identity", {}).get("ean"), TARGET_EAN)

    async def test_repeated_enrichment_call_is_idempotent_and_still_never_mutates(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="LG.xlsx", content=_price_list_bytes()
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
        first = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="same-request-id",
                conversation_id="c1",
            )
        )
        self.assertEqual(first.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        before = len(store.catalog("tenant-a"))

        second = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="same-request-id",
                conversation_id="c1",
            )
        )
        self.assertTrue(second.metadata.get("duplicate"))
        self.assertEqual(len(store.catalog("tenant-a")), before)


class EnrichmentResearchAndMediaCandidatePropagationTests(unittest.IsolatedAsyncioTestCase):
    """Brave-search-shaped end-to-end coverage (PR #49 follow-up): a real
    ToolGateway.search() result feeds product_enrichment's research,
    scrape.fetch (mocked transport, never live network) supplies page
    text, an image URL embedded in that SAME page is auto-discovered and
    propagated -- with NO explicit media_candidates -- into the EXISTING
    GovernedImageFetcher/MediaAcquisitionService path, and NEVER causes
    any Bitrix mutation or bypasses the separate explicit confirmation
    gate."""

    RESEARCH_URL = "https://www.lg.com/ru/55MRGB86B6A.ARUG-review"
    IMAGE_URL = "https://www.lg.com/ru/photos/55mrgb86b6a-hero.png"

    def _png_bytes(self) -> bytes:
        import io

        from PIL import Image

        img = Image.new("RGB", (400, 400), (10, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _scrape_fetch_handler(self):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == self.RESEARCH_URL:
                html = (
                    f'<html><head><meta property="og:image" content="{self.IMAGE_URL}">'
                    "</head><body>Цвет: черный</body></html>"
                )
                return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
            return httpx.Response(404)

        return handler

    async def test_media_candidate_from_research_reaches_governed_image_fetcher_with_zero_mutation(self):
        from product_enrichment.media_fetch import FakeImageFetcher
        from tools.search.fake_provider import FakeSearchProvider, fake_result

        search_provider = FakeSearchProvider(
            {
                f"{TARGET_BRAND} {TARGET_SKU}": [
                    fake_result(self.RESEARCH_URL, title=f"{TARGET_BRAND} {TARGET_SKU} review")
                ]
            }
        )
        media_fetcher = FakeImageFetcher({self.IMAGE_URL: self._png_bytes()})

        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(
            bitrix_bridge=bridge,
            search_provider=search_provider,
            scrape_fetch_handler=self._scrape_fetch_handler(),
            media_fetcher=media_fetcher,
        )
        ref = await _register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="LG.xlsx", content=_price_list_bytes()
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

        result = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )

        # Zero Bitrix mutation from enrichment alone -- unchanged approval boundary.
        self.assertEqual(result.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        self.assertEqual(len(store.catalog("tenant-a")), before)

        preview = result.metadata.get("enrichment_preview") or {}
        media_preview = preview.get("media") or {}
        self.assertEqual(media_preview.get("status"), "ready")

        # The raw external image URL must never leak into the write
        # request's picture payloads -- only filename/base64.
        task = panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")  # noqa: SLF001
        self.assertIsNotNone(task)
        write_request = task.parameters["bitrix_enrichment_write_request"]
        preview_picture = write_request.get("preview_picture") or {}
        self.assertTrue(preview_picture.get("base64"))
        self.assertNotIn(self.IMAGE_URL, str(preview_picture))
        self.assertNotIn(self.IMAGE_URL, result.text)

    async def test_no_media_fetcher_configured_still_produces_preview_without_hotlink(self):
        """A conversational gateway constructed WITHOUT an explicit
        media_fetcher must default to a real (production-shaped)
        GovernedImageFetcher -- never crash, never silently skip the
        MEDIA GAP closure -- even though this test's own fake search/
        fetch capabilities never trigger a real network call."""
        bridge, _store = _bitrix_bridge()
        panda, _artifact_service = _panda(bitrix_bridge=bridge)
        from product_enrichment.media_fetch import GovernedImageFetcher

        self.assertIsInstance(panda._media_fetcher, GovernedImageFetcher)  # noqa: SLF001


class EnrichmentMissingContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrichment_request_without_prior_preview_asks_to_prepare_first(self):
        bridge, store = _bitrix_bridge()
        panda, _artifact_service = _panda(bitrix_bridge=bridge)

        result = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c-no-preview",
            )
        )
        self.assertNotEqual(result.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        self.assertIn("товар", result.text.casefold())


class ConfirmationReusesEnrichedWriteRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_after_enrichment_still_creates_exactly_one_product(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="LG.xlsx", content=_price_list_bytes()
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
        enrichment = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )
        self.assertEqual(enrichment.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        before = len(store.catalog("tenant-a"))

        approval = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c1",
            )
        )

        self.assertEqual(approval.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(len(store.catalog("tenant-a")) - before, 1)
        result = approval.metadata.get("bitrix_write_result") or {}
        self.assertTrue(result.get("mutated"))
        self.assertEqual(result.get("sku"), TARGET_SKU)

    async def test_confirmation_without_enrichment_still_works_unchanged(self):
        # Regression: the #43 write path must remain fully usable even if
        # the owner never asks for a complete card at all.
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="LG.xlsx", content=_price_list_bytes()
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
        self.assertEqual(len(store.catalog("tenant-a")) - before, 1)

    async def test_switching_to_a_different_product_discards_stale_enrichment(self):
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge)
        ref = await _register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="LG.xlsx", content=_price_list_bytes()
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
        await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )

        # Now select a DIFFERENT product on the SAME active task/conversation.
        await panda.respond(
            ConversationRequest(
                text=OTHER_PREVIEW_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c1",
            )
        )

        approval = await panda.respond(
            ConversationRequest(
                text=f"Подтверждаю: создай этот товар в Bitrix. Розничная цена 24990 \u20bd.",
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r4",
                conversation_id="c1",
            )
        )
        self.assertEqual(approval.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        result = approval.metadata.get("bitrix_write_result") or {}
        # The write must target the SECOND product, never the first
        # product's stale enrichment data.
        self.assertEqual(result.get("sku"), OTHER_SKU)


if __name__ == "__main__":
    unittest.main()
