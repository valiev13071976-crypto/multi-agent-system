"""Production defect closure: an explicit write confirmation for an ALREADY
prepared product card must execute the EXISTING governed single-product
Bitrix write, not re-print the prepared card.

Exact reproduced case:

    turn 1: "Подготовь полную карточку товара ..."  (CALL_PRODUCT_ENRICHMENT,
            unchanged) -> prepared write request persisted on the task
    turn 2: "Подтверждаю запись. Выполни запись этого товара в Bitrix/Aspro.
            Розничная цена ... ₽."

Before the fix, turn 2 matched no Bitrix branch at all (the confirmation
predicate only recognised imperative create verbs -- "создай"/"запиши" --
not the noun form owners actually use), so it fell through to the
FAMILY_EXCEL continuation and echoed the prepared product-card preview back
with zero write.

FIXTURE Bitrix adapter only -- zero live Bitrix mutations.
"""

from __future__ import annotations

import unittest

from business_assistant.action_continuation import CALL_CONTROLLED_BITRIX_WRITE
from business_assistant.conversation_gateway import ConversationRequest
from business_assistant.intent import is_conversational
from tests.test_panda_product_enrichment_conversational import (
    ENRICHMENT_TURN_TEXT,
    PREVIEW_TURN_TEXT,
    TARGET_BRAND,
    TARGET_SKU,
    USER_RETAIL_PRICE_RUB,
    _bitrix_bridge,
    _panda,
    _price_list_bytes,
    _register_upload,
)
from tools.search.fake_provider import FakeSearchProvider, fake_result

RESEARCH_URL = "https://www.lg.com/ru/tv/55mrgb86b6a"

# The confirmation wording that reproduced the defect: an explicit approval
# marker + Bitrix/Aspro target + the ACTION AS A NOUN, with no imperative
# "создай"/"запиши" anywhere.
NOUN_FORM_CONFIRMATION_TEXT = (
    "Подтверждаю запись. Выполни запись этого товара в Bitrix/Aspro. "
    f"Розничная цена {USER_RETAIL_PRICE_RUB} \u20bd."
)

# The byte-exact production confirmation that STILL did not reach the
# governed write after the noun-form phrases above were added: it qualifies
# the noun ("реальную запись"), so no adjacent verb+noun phrase matched, and
# it carries no retail price of its own (the prepared card's persisted
# preview price must be reused).
PRODUCTION_CONFIRMATION_TEXT = (
    "Подтверждаю. Выполни реальную запись этого подготовленного товара в Bitrix/Aspro."
)


class ExplicitWriteConfirmationExecutesGovernedWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_noun_form_confirmation_writes_the_prepared_card_once(self):
        self.assertTrue(is_conversational(NOUN_FORM_CONFIRMATION_TEXT))

        search_provider = FakeSearchProvider(
            {f"{TARGET_BRAND} {TARGET_SKU}": [fake_result(RESEARCH_URL, title=f"LG {TARGET_SKU}")]}
        )
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge, search_provider=search_provider)
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
        task = panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")  # noqa: SLF001
        prepared = dict(task.parameters["bitrix_enrichment_write_request"])
        self.assertTrue(prepared.get("sku"))
        searches_after_enrichment = len(search_provider.queries)
        before = len(store.catalog("tenant-a"))

        # Capture what the unchanged write path actually hands to Bitrix.
        written_canonical: dict = {}
        real_sync_product = bridge.sync_product

        def _spy_sync_product(**kwargs):
            written_canonical.update(kwargs.get("canonical_product") or {})
            return real_sync_product(**kwargs)

        bridge.sync_product = _spy_sync_product

        confirmation = await panda.respond(
            ConversationRequest(
                text=NOUN_FORM_CONFIRMATION_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c1",
            )
        )

        # The existing governed single-product write ran -- not another
        # product-card preview echo.
        self.assertEqual(confirmation.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertNotIn("жду вашего подтверждения", confirmation.text)
        write_result = confirmation.metadata.get("bitrix_write_result") or {}
        self.assertTrue(write_result.get("mutated"))
        self.assertEqual(write_result.get("sku"), TARGET_SKU)
        self.assertEqual(len(store.catalog("tenant-a")) - before, 1)

        # It wrote the ALREADY prepared (enriched) request, and neither
        # search nor enrichment ran again for this turn.
        self.assertEqual(len(search_provider.queries), searches_after_enrichment)
        self.assertEqual(written_canonical.get("sku"), prepared.get("sku"))
        content = written_canonical.get("content") or {}
        self.assertEqual(content.get("short_description"), prepared.get("short_description"))
        self.assertEqual(content.get("detailed_description"), prepared.get("detailed_description"))
        self.assertEqual(
            (written_canonical.get("price") or {}).get("selling_price"), USER_RETAIL_PRICE_RUB
        )

        # Not writable a second time: the real blocking reason from the
        # unchanged write path, still zero further mutations.
        after_first = len(store.catalog("tenant-a"))
        repeat = await panda.respond(
            ConversationRequest(
                text=NOUN_FORM_CONFIRMATION_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r4",
                conversation_id="c1",
            )
        )
        self.assertEqual(repeat.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(len(store.catalog("tenant-a")), after_first)
        self.assertIn("уже существует", repeat.text)


class ProductionQualifiedNounConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_confirmation_text_executes_the_governed_write_exactly_once(self):
        search_provider = FakeSearchProvider(
            {f"{TARGET_BRAND} {TARGET_SKU}": [fake_result(RESEARCH_URL, title=f"LG {TARGET_SKU}")]}
        )
        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(bitrix_bridge=bridge, search_provider=search_provider)
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
        task = panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")  # noqa: SLF001
        prepared = dict(task.parameters["bitrix_enrichment_write_request"])
        searches_after_enrichment = len(search_provider.queries)
        before = len(store.catalog("tenant-a"))

        writes: list[dict] = []
        real_sync_product = bridge.sync_product

        def _spy_sync_product(**kwargs):
            writes.append(dict(kwargs.get("canonical_product") or {}))
            return real_sync_product(**kwargs)

        bridge.sync_product = _spy_sync_product

        confirmation = await panda.respond(
            ConversationRequest(
                text=PRODUCTION_CONFIRMATION_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c1",
            )
        )

        self.assertEqual(confirmation.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertNotIn("жду вашего подтверждения", confirmation.text)
        # The existing governed write ran exactly once.
        self.assertEqual(len(writes), 1)
        self.assertTrue((confirmation.metadata.get("bitrix_write_result") or {}).get("mutated"))
        self.assertEqual(len(store.catalog("tenant-a")) - before, 1)

        # It wrote the persisted prepared card, reusing the retail price
        # captured earlier (this message states none), and neither search
        # nor enrichment ran again on the confirmation turn.
        canonical = writes[0]
        self.assertEqual(canonical.get("sku"), prepared.get("sku"))
        content = canonical.get("content") or {}
        self.assertEqual(content.get("short_description"), prepared.get("short_description"))
        self.assertEqual(content.get("detailed_description"), prepared.get("detailed_description"))
        self.assertEqual((canonical.get("price") or {}).get("selling_price"), USER_RETAIL_PRICE_RUB)
        self.assertEqual(len(search_provider.queries), searches_after_enrichment)


if __name__ == "__main__":
    unittest.main()
