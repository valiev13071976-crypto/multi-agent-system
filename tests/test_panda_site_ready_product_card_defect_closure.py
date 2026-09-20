"""PRODUCT-FIRST DEFECT CLOSURE: any ordinary business-language request to
prepare/add/publish a product to the site resolves to the SAME end goal --
a complete, site-ready product card -- without the user ever having to
utter an internal workflow term ("enrichment"/"обогащение"/"SEO"/
"характеристики"/"галерея").

Reproduced production defect:

    select product -> set retail price -> "покажи план записи этого
    товара в Bitrix" -> confirm write

built a bare ``SingleProductWriteRequest`` straight from the spreadsheet
row (``business_assistant.controlled_bitrix_write.
build_write_request_from_fields``): 0 characteristics, no images, no
description -- even though the EXISTING ``product_enrichment_bridge``
pipeline already supports all of them. The user was expected to separately
say "Подготовь полную карточку..." for the SAME product before asking to
see/confirm the write plan.

ROOT CAUSE / SEAM: ``WorkflowPandaConversationGateway._explain_bitrix_
write_plan`` (the read-only preview) and ``_invoke_controlled_bitrix_write``
(the governed write) both read ``ActiveTask.parameters[
'bitrix_enrichment_write_request']`` -- populated ONLY by an explicit
``CALL_PRODUCT_ENRICHMENT`` turn -- and fell straight back to
``build_write_request_from_fields`` (raw spreadsheet fields only) whenever
that state was absent, regardless of WHICH deterministic predicate in
``business_assistant.action_continuation`` actually routed the turn there
(``is_bitrix_write_plan_question``, the generic "final plan" follow-up
fallback, or an explicit write confirmation with no prior preview turn at
all).

FIX (reuses the EXISTING ``product_enrichment_bridge``/governed-write
machinery -- no new pipeline, no new agent/router/store):
``WorkflowPandaConversationGateway._auto_prepare_site_ready_card_if_needed``
is the ONE seam both handlers call BEFORE building/reusing a write
request. Whenever a single product is already selected
(``bitrix_product_fields['sku']``) but no card has been prepared yet, it
runs the EXISTING ``product_enrichment_bridge.prepare_complete_card``
pipeline exactly once and persists the result onto the SAME
``ActiveTask`` -- reusing the EXISTING ``CALL_PRODUCT_ENRICHMENT`` state
shape. Already-prepared state is reused as-is (idempotent no-op, never
re-run). Explicit user limits ("только данные из прайса", "без картинок",
"без описания") always win -- see ``business_assistant.action_continuation.
has_explicit_price_list_only_constraint``/``has_explicit_no_media_
constraint``/``has_explicit_no_description_constraint``.

This module proves BOTH mandatory acceptance scenarios end to end through
a REAL ``WorkflowPandaConversationGateway`` wired to a FIXTURE Bitrix
bridge, a fake web-search/scrape research backend and a fake image
fetcher (so the "complete card" claim is backed by REAL characteristics/
images/descriptions actually produced by the pipeline, never an empty
stub) -- Cursor performs ZERO live/real network or Bitrix mutations while
implementing/testing this:

1. ``NaturalJourneyProducesCompleteSiteReadyCardTests`` -- the required
   natural journey (upload -> analyze -> select -> set retail price ->
   "покажи, что будет записано на сайт" -> auto-prepared complete card ->
   confirm -> governed write -> read-back), asserting NONE of the user's
   OWN messages contain an internal workflow term, yet the card still
   comes back complete.

2. ``ExplicitUserConstraintTests`` -- "только данные из прайса, без
   картинок и описания" skips ALL auto-preparation outright (bare
   spreadsheet-only card, unchanged pre-fix shape); "без картинок и
   описания" alone (no price-list-only qualifier) still runs the pipeline
   and still produces characteristics, but omits images/descriptions.
"""

from __future__ import annotations

import io
import unittest

import httpx
from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CALL_CONTROLLED_BITRIX_WRITE, EXPLAIN_BITRIX_WRITE_PLAN
from business_assistant.conversation_gateway import ConversationRequest, WorkflowPandaConversationGateway
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter
from integrations.bitrix.product_bridge import BitrixProductBridge
from product_enrichment.media_fetch import FakeImageFetcher
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry
from tools.search.fake_provider import FakeSearchProvider, fake_result

TARGET_SKU = "55MRGB86B6A.ARUG"
TARGET_PRODUCT_NAME = f"LG {TARGET_SKU}"
TARGET_CATEGORY = "TV"
TARGET_BRAND = "LG"
TARGET_EAN = "8806096824788"
TARGET_PURCHASE_PRICE = "103198.3"
TARGET_RETAIL_PRICE = "139990"
FILENAME = "LG_price_list.xlsx"

RESEARCH_URL = f"https://www.lg.com/ru/{TARGET_SKU}-review"
IMAGE_URL = "https://www.lg.com/ru/photos/55mrgb86b6a-hero.png"

# Internal workflow terms the user must NEVER be required to utter (task's
# own literal list, checked case-insensitively across every message the
# TEST sends as the "user").
FORBIDDEN_USER_WORDS = ("enrichment", "обогащен", "обогати", "seo", "характеристик", "галере")

UPLOAD_TURN_TEXT = "Вот прайс-лист поставщика, посмотри, что там есть."
SELECT_TURN_TEXT = f"Найди товар LG {TARGET_SKU}."
SET_PRICE_TURN_TEXT = f"Установи розничную цену {TARGET_RETAIL_PRICE} рублей для этого товара."
SHOW_SITE_TURN_TEXT = "Покажи, что будет записано на сайт."
CONFIRM_TURN_TEXT = "Подтверждаю: создай этот товар в Bitrix."


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price"])
    ws.append([TARGET_SKU, TARGET_PRODUCT_NAME, TARGET_CATEGORY, TARGET_BRAND, TARGET_EAN, TARGET_PURCHASE_PRICE])
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


def _png_bytes() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (400, 400), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scrape_fetch_handler(request: httpx.Request) -> httpx.Response:
    """A real manufacturer product page: an og:image (the media-gap
    closure's own discovery mechanism) plus two STRUCTURAL spec lines
    (``<li>label: value</li>``, exactly the shape
    ``product_enrichment.characteristics.extract_spec_lines`` parses) --
    never fabricated by the test framework, only what a real page would
    literally contain."""
    if str(request.url) == RESEARCH_URL:
        html = (
            f'<html><head><meta property="og:image" content="{IMAGE_URL}"></head>'
            "<body><ul><li>Цвет: черный</li><li>Диагональ экрана: 55 см</li></ul></body></html>"
        )
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
    return httpx.Response(404)


def _panda_with_research_backend() -> tuple[WorkflowPandaConversationGateway, ArtifactService, BitrixCatalogStore]:
    """A conversational gateway wired to a REAL (fixture-mode) Bitrix
    bridge plus a fake web-search/scrape/image-fetch research backend --
    so "the complete card" is proven by REAL characteristics/images/
    descriptions the pipeline actually produced, never an artificially
    empty stub."""
    svc = DataIntelligenceService(InMemoryDatasetStore())
    artifact_service = ArtifactService(store=InMemoryArtifactStore())
    svc.artifact_service = artifact_service
    registry = ToolRegistry()
    register_platform_tools(registry, data_intelligence=svc)
    adapters = {row.descriptor.tool_id: row.adapter for row in registry._items.values()}  # noqa: SLF001
    adapters["scrape.fetch"]._transport = httpx.MockTransport(_scrape_fetch_handler)  # noqa: SLF001
    search_provider = FakeSearchProvider(
        {f"{TARGET_BRAND} {TARGET_SKU}": [fake_result(RESEARCH_URL, title=f"{TARGET_BRAND} {TARGET_SKU} review")]}
    )
    gateway = ToolGateway(registry=registry, register_search=False, search_provider=search_provider)
    media_fetcher = FakeImageFetcher({IMAGE_URL: _png_bytes()})
    bridge, store = _bitrix_bridge()
    panda = WorkflowPandaConversationGateway(
        workflow_engine=object(),
        run_router=object(),
        context_manager=object(),
        tool_gateway=gateway,
        artifact_service=artifact_service,
        bitrix_product_bridge=bridge,
        media_fetcher=media_fetcher,
    )
    return panda, artifact_service, store


async def _register_upload(artifact_service: ArtifactService, *, tenant: str, owner: str, conv: str) -> str:
    rec = artifact_service.register_upload(tenant_id=tenant, owner_id=owner, filename=FILENAME, content=_xlsx_bytes())
    artifact_service.attach_to_conversation(tenant_id=tenant, artifact_id=rec.artifact_id, conversation_id=conv)
    return rec.artifact_id


class NaturalJourneyProducesCompleteSiteReadyCardTests(unittest.IsolatedAsyncioTestCase):
    """MANDATORY ACCEPTANCE (natural journey): upload price -> analyze ->
    select any product naturally -> set retail price -> "покажи, что
    будет записано на сайт" -> Panda automatically prepares the complete
    card -> preview includes available characteristics, descriptions,
    images -> user confirms -> governed Bitrix write -> read-back. No
    user message contains "enrichment"/"обогащение"/"SEO"/
    "характеристики"/"галерея"."""

    async def test_natural_journey_upload_to_governed_write_produces_complete_card(self):
        panda, artifact_service, store = _panda_with_research_backend()
        user_messages = [
            UPLOAD_TURN_TEXT,
            SELECT_TURN_TEXT,
            SET_PRICE_TURN_TEXT,
            SHOW_SITE_TURN_TEXT,
            CONFIRM_TURN_TEXT,
        ]

        # The task's own core requirement: the user's business language
        # never needs an internal workflow term for the full card to come
        # back anyway.
        for message in user_messages:
            blob = message.casefold()
            for forbidden in FORBIDDEN_USER_WORDS:
                self.assertNotIn(
                    forbidden,
                    blob,
                    f"user message {message!r} must never need to say {forbidden!r}",
                )

        ref = await _register_upload(artifact_service, tenant="tenant-a", owner="user-a", conv="conv-1")
        before_catalog_size = len(store.catalog("tenant-a"))

        # Turn 1: upload + analyze.
        upload_result = await panda.respond(
            ConversationRequest(
                text=UPLOAD_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r1",
                conversation_id="conv-1",
                attachment_refs=(ref,),
            )
        )
        self.assertTrue(upload_result.text)

        # Turn 2: select a product naturally (no "enrichment" wording).
        select_result = await panda.respond(
            ConversationRequest(
                text=SELECT_TURN_TEXT, tenant_id="tenant-a", user_id="user-a", request_id="r2", conversation_id="conv-1"
            )
        )
        self.assertIn(TARGET_SKU, select_result.text)

        # Turn 3: set the retail price for the ALREADY selected product.
        price_result = await panda.respond(
            ConversationRequest(
                text=SET_PRICE_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r3",
                conversation_id="conv-1",
            )
        )
        self.assertIn(TARGET_RETAIL_PRICE, price_result.text)

        # Turn 4: "покажи, что будет записано на сайт" -- the ordinary
        # business-language ask this whole defect closure is about. No
        # separate enrichment command was ever issued.
        site_preview = await panda.respond(
            ConversationRequest(
                text=SHOW_SITE_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r4",
                conversation_id="conv-1",
            )
        )
        self.assertEqual(site_preview.metadata.get("action_decision"), EXPLAIN_BITRIX_WRITE_PLAN)
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ: 2", site_preview.text)
        self.assertIn("color: черный", site_preview.text)
        self.assertIn("screen_diagonal_cm: 55", site_preview.text)
        self.assertIn("превью:", site_preview.text)
        self.assertIn("детальное:", site_preview.text)
        self.assertNotIn("нет подготовленных изображений", site_preview.text)
        self.assertIn("Короткое (previewText): LG", site_preview.text)
        self.assertNotIn("Короткое (previewText): (нет)", site_preview.text)
        self.assertIn(TARGET_RETAIL_PRICE, site_preview.text)

        # The complete card was auto-prepared and persisted -- reused, not
        # rebuilt, by the confirmation turn below.
        task = panda._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        enriched = dict(task.parameters.get("bitrix_enrichment_write_request") or {})
        self.assertTrue(enriched)
        self.assertEqual(enriched.get("sku"), TARGET_SKU)
        self.assertTrue(enriched.get("characteristics"))
        self.assertTrue(enriched.get("preview_picture"))

        # Turn 5: explicit governed confirmation -- the ONLY step allowed
        # to mutate Bitrix.
        self.assertEqual(len(store.catalog("tenant-a")), before_catalog_size)
        confirm_result = await panda.respond(
            ConversationRequest(
                text=CONFIRM_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r5",
                conversation_id="conv-1",
            )
        )
        self.assertEqual(confirm_result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        write_result = confirm_result.metadata.get("bitrix_write_result") or {}
        self.assertTrue(write_result.get("mutated"))
        self.assertEqual(write_result.get("sku"), TARGET_SKU)
        self.assertEqual(len(store.catalog("tenant-a")) - before_catalog_size, 1)

        # Read-back: the write result confirms what was actually recorded,
        # including the enriched fields (never a re-echo of the bare
        # request).
        self.assertIn(TARGET_SKU, confirm_result.text)
        self.assertIn(TARGET_RETAIL_PRICE, confirm_result.text)
        self.assertIn("подтверждена", confirm_result.text.casefold())


class ExplicitUserConstraintTests(unittest.IsolatedAsyncioTestCase):
    """MANDATORY ACCEPTANCE (constraint test): explicit user limits always
    win over the default "complete card" expectation."""

    async def _select_and_price(self, panda: WorkflowPandaConversationGateway, artifact_service: ArtifactService):
        ref = await _register_upload(artifact_service, tenant="tenant-a", owner="user-a", conv="conv-1")
        await panda.respond(
            ConversationRequest(
                text=UPLOAD_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r1",
                conversation_id="conv-1",
                attachment_refs=(ref,),
            )
        )
        await panda.respond(
            ConversationRequest(
                text=SELECT_TURN_TEXT, tenant_id="tenant-a", user_id="user-a", request_id="r2", conversation_id="conv-1"
            )
        )
        await panda.respond(
            ConversationRequest(
                text=SET_PRICE_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r3",
                conversation_id="conv-1",
            )
        )

    async def test_price_list_only_constraint_skips_full_preparation(self):
        """"только данные из прайса, без картинок и описания" -> the whole
        auto-preparation stage is skipped outright; the card stays exactly
        the bare spreadsheet row (unchanged pre-fix shape) -- NO
        characteristics/images/description are ever prepared, even though
        the SAME wired research backend would otherwise have produced
        them (proven by ``NaturalJourneyProducesCompleteSiteReadyCardTests``
        above with the identical fixture)."""
        panda, artifact_service, _store = _panda_with_research_backend()
        await self._select_and_price(panda, artifact_service)

        result = await panda.respond(
            ConversationRequest(
                text=(
                    f"{SHOW_SITE_TURN_TEXT} Только данные из прайса, без картинок и описания."
                ),
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r4",
                conversation_id="conv-1",
            )
        )
        self.assertEqual(result.metadata.get("action_decision"), EXPLAIN_BITRIX_WRITE_PLAN)
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ: 0", result.text)
        self.assertIn("нет подготовленных изображений", result.text)
        self.assertIn("Короткое (previewText): (нет)", result.text)

        task = panda._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        self.assertFalse(dict(task.parameters.get("bitrix_enrichment_write_request") or {}))

    async def test_no_media_no_description_alone_skips_only_those_stages(self):
        """"без картинок и описания" (no "только данные из прайса"
        qualifier) still runs the full preparation pipeline -- proven by
        non-empty characteristics below, from the SAME wired research
        backend -- but omits images/descriptions specifically."""
        panda, artifact_service, _store = _panda_with_research_backend()
        await self._select_and_price(panda, artifact_service)

        result = await panda.respond(
            ConversationRequest(
                text=f"{SHOW_SITE_TURN_TEXT} Без картинок и описания.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r4",
                conversation_id="conv-1",
            )
        )
        self.assertEqual(result.metadata.get("action_decision"), EXPLAIN_BITRIX_WRITE_PLAN)

        task = panda._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        enriched = dict(task.parameters.get("bitrix_enrichment_write_request") or {})
        self.assertTrue(enriched)
        # Characteristics still fully prepared -- the constraint never
        # touched that stage.
        self.assertTrue(enriched.get("characteristics"))
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ: 2", result.text)
        # Images and descriptions specifically omitted.
        self.assertFalse(enriched.get("preview_picture"))
        self.assertFalse(enriched.get("detail_picture"))
        self.assertFalse(enriched.get("gallery_pictures"))
        self.assertEqual(enriched.get("short_description"), "")
        self.assertEqual(enriched.get("detailed_description"), "")
        self.assertIn("нет подготовленных изображений", result.text)
        self.assertIn("Короткое (previewText): (нет)", result.text)


if __name__ == "__main__":
    unittest.main()
