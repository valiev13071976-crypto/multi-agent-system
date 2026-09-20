"""PRODUCT-FIRST ROUTING DEFECT CLOSURE (continuation of #102): ordinary
business-language requests meaning "show me what will be written/
published/uploaded to the site/Bitrix" for an ALREADY SELECTED product
must all converge on the SAME action family (``EXPLAIN_BITRIX_WRITE_PLAN``)
-- never one literal trigger phrase.

Reported gap: "покажи, что будет записано на сайт" (worked) and "покажи
план записи этого товара в Bitrix" (fell back to a plain row preview)
express the SAME business intent, yet only the first satisfied
``is_bitrix_write_plan_question``/``_is_write_plan_ask``'s narrow "будет
записан"/"final plan" regex.

ROOT CAUSE: ``_WRITE_PLAN_RE`` only recognises a small, literal set of
"will be written"/"final plan" phrasings. Both callers that dispatch to
``EXPLAIN_BITRIX_WRITE_PLAN`` --
``is_bitrix_write_plan_question`` (requires an explicit "Bitrix"/"Aspro"
mention in the SAME turn) and the generic "already active, already-
selected product" continuation fallback in ``resolve_action_turn`` (no
target-mention required, but only reachable once a product from an
EARLIER turn is already on the active task) -- shared that one regex.

WHY THE WORDING DEPENDENCY EXISTED: ``is_bitrix_write_plan_question``
cannot safely be broadened in place -- it has no "product already
selected on an earlier turn" guard of its own, so a broader match would
also fire on a brand-new Turn-1 message that attaches a fresh spreadsheet
and asks to "...покажи мне подготовленную карточку и план действий перед
записью" in the SAME breath (see
``test_panda_xlsx_attachment_defect_closure.py`` and siblings) -- there is
no active task yet for the resolver to describe, so broadening THAT
predicate's own vocabulary risks turning a normal first-turn ingestion
into a "missing context" failure.

MINIMAL GENERIC FIX: a new, deliberately broader predicate,
``business_assistant.action_continuation.is_read_only_write_preview_ask``,
used at exactly ONE call site -- the existing generic continuation
fallback, which ALREADY requires an active ``FAMILY_EXCEL`` task with a
resolved product (``bitrix_product_fields['sku']``) from an earlier turn.
That pre-existing guard is what makes the broader vocabulary
(показать/объяснить/what-will/что-собираешься + запис/план/карточ/итог/
результат/загруз/отправ/публикац/upload/send/plan/result/card) safe: it
structurally can never fire on a fresh Turn-1 attachment turn, so
``is_bitrix_write_plan_question``'s own narrow behaviour (and every test
pinned to it) is untouched. No growing phrase-specific regex list --
composable semantic stems (a "show/ask" signal + a "write/publish"
concept), gated by the SAME "product already selected" precondition the
task itself calls out as the natural scope boundary.

This module proves the fix end to end (real gateway, real fixture Bitrix
bridge, fake research backend -- zero live network/Bitrix calls) for the
5 mandatory scenarios, plus direct predicate-level coverage of the wider
"equivalent intent class" examples from the task description.
"""

from __future__ import annotations

import unittest

from business_assistant.action_continuation import (
    CALL_CONTROLLED_BITRIX_WRITE,
    EXPLAIN_BITRIX_WRITE_PLAN,
    is_bitrix_write_plan_question,
    is_explicit_bitrix_write_confirmation,
    is_read_only_write_preview_ask,
)
from business_assistant.conversation_gateway import ConversationRequest, WorkflowPandaConversationGateway
from tests.test_panda_site_ready_product_card_defect_closure import (
    CONFIRM_TURN_TEXT,
    SELECT_TURN_TEXT,
    SET_PRICE_TURN_TEXT,
    SHOW_SITE_TURN_TEXT,
    TARGET_RETAIL_PRICE,
    TARGET_SKU,
    UPLOAD_TURN_TEXT,
    _panda_with_research_backend,
    _register_upload,
)

# The exact reported-broken phrase, plus equivalent-intent variants that
# never repeat its literal "план записи" wording -- deliberately never a
# growing production phrase list, only mandatory TEST fixtures exercising
# the generic semantic seam.
PREVIEW_VARIANT_LITERAL_PLAN = "Покажи план записи этого товара в Bitrix."
PREVIEW_VARIANT_ENGLISH = "Show me what's going to be uploaded to Bitrix for this product."
PREVIEW_VARIANT_NO_PLAN_WORDING = "Что ты собираешься отправить на сайт?"

# Broader "equivalent intent class" examples straight from the task
# description -- unit-level predicate coverage only (no growing production
# regex, just proving the composable semantic stems recognise all of
# them).
EQUIVALENT_INTENT_EXAMPLES = (
    "Покажи, что будет записано на сайт.",
    "Покажи план записи товара.",
    "Что будет загружено в Bitrix?",
    "Покажи карточку перед записью.",
    "Что ты собираешься отправить на сайт?",
    "Покажи итог перед публикацией.",
    "Show me the write plan before you publish it.",
)

# A message that must NEVER be mistaken for a read-only preview -- an
# actual governed write confirmation always wins.
NEGATIVE_CONFIRMATION_EXAMPLES = (
    CONFIRM_TURN_TEXT,
    "Подтверждаю запись этого товара в Bitrix/Aspro по показанному плану.",
    "Подтверждаю, записывай в Bitrix как показано.",
)

# Block 5.5's own protected first-turn row-preview phrasing (see
# ``test_panda_xlsx_attachment_defect_closure.py``) -- proves the
# BROADER-vocabulary predicate itself does not silently swallow this
# shape; it stays safe purely because of the call site's "already
# selected on an earlier turn" guard, exercised end-to-end by the
# untouched, still-passing #102 xlsx-attachment test suite.
BLOCK_5_5_PROTECTED_PHRASE = (
    "Найди товар LG 32LQ63006LA.ARUG и подготовь его для добавления в Bitrix/Aspro Premier. "
    "Сначала покажи мне подготовленную карточку и план действий перед записью."
)


class ReadOnlyWritePreviewAskPredicateTests(unittest.TestCase):
    """Unit-level coverage of the new generic predicate: every equivalent
    intent-class example matches, no exact wording dependency, and actual
    confirmations never match."""

    def test_all_equivalent_intent_examples_match(self):
        for text in EQUIVALENT_INTENT_EXAMPLES:
            with self.subTest(text=text):
                self.assertTrue(is_read_only_write_preview_ask(text), text)

    def test_confirmations_never_match_the_preview_predicate(self):
        for text in NEGATIVE_CONFIRMATION_EXAMPLES:
            with self.subTest(text=text):
                self.assertTrue(is_explicit_bitrix_write_confirmation(text), text)

    def test_bare_write_command_without_a_show_ask_does_not_match(self):
        # "отправ"/"публикац" alone (an imperative DO-IT, not a "show me"
        # ask) must never be treated as a read-only preview.
        self.assertFalse(is_read_only_write_preview_ask("Отправь товар на сайт."))
        self.assertFalse(is_read_only_write_preview_ask("Опубликуй товар в Bitrix."))

    def test_block_5_5_protected_first_turn_phrase_is_unaffected(self):
        # ``is_bitrix_write_plan_question`` itself keeps its OLD, narrow
        # behaviour -- untouched by the new, broader predicate below.
        self.assertFalse(is_bitrix_write_plan_question(BLOCK_5_5_PROTECTED_PHRASE))
        # The new predicate's own vocabulary WOULD match this text in
        # isolation (it does mention "карточку"/"план"+"записью" alongside
        # "покажи") -- proving the safety net is the call site's "product
        # already selected on an EARLIER turn" guard, not the predicate's
        # own wording, exactly as documented above.
        self.assertTrue(is_read_only_write_preview_ask(BLOCK_5_5_PROTECTED_PHRASE))


class SemanticWritePreviewRoutingDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """MANDATORY ACCEPTANCE: parameterized natural-language preview
    variants, run end to end through the real gateway, all reach the SAME
    action family for the SAME selected product; site-ready preparation
    runs once if needed and is reused; zero write happens."""

    async def _select_and_price(self, panda: WorkflowPandaConversationGateway, artifact_service):
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

    async def _assert_variant_reaches_write_plan_preview(self, variant_text: str):
        panda, artifact_service, store = _panda_with_research_backend()
        await self._select_and_price(panda, artifact_service)
        before_catalog_size = len(store.catalog("tenant-a"))

        result = await panda.respond(
            ConversationRequest(
                text=variant_text,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r4",
                conversation_id="conv-1",
            )
        )

        # Same action family, regardless of the exact wording used.
        self.assertEqual(result.metadata.get("action_decision"), EXPLAIN_BITRIX_WRITE_PLAN, variant_text)

        # Same selected product; site-ready preparation ran (real
        # characteristics from the wired research backend, never an empty
        # stub) and was persisted onto the SAME task.
        task = panda._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        self.assertEqual(dict(task.parameters.get("bitrix_product_fields") or {}).get("sku"), TARGET_SKU)
        enriched = dict(task.parameters.get("bitrix_enrichment_write_request") or {})
        self.assertTrue(enriched, variant_text)
        self.assertEqual(enriched.get("sku"), TARGET_SKU)
        self.assertTrue(enriched.get("characteristics"), variant_text)
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ: 2", result.text)

        # Zero write: Bitrix catalog untouched.
        self.assertEqual(len(store.catalog("tenant-a")), before_catalog_size, variant_text)

        # Persisted prepared card is REUSED, not rebuilt, by a second,
        # differently-worded preview ask in the SAME conversation.
        second = await panda.respond(
            ConversationRequest(
                text=SHOW_SITE_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r5",
                conversation_id="conv-1",
            )
        )
        self.assertEqual(second.metadata.get("action_decision"), EXPLAIN_BITRIX_WRITE_PLAN)
        task_after = panda._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        self.assertEqual(
            dict(task_after.parameters.get("bitrix_enrichment_write_request") or {}),
            enriched,
            "the same prepared card must be reused, not re-prepared, by a follow-up preview ask",
        )
        self.assertEqual(len(store.catalog("tenant-a")), before_catalog_size)

    async def test_baseline_phrase_still_reaches_write_plan_preview(self):
        # Test 1 -- must keep working (pre-existing #102 behaviour).
        await self._assert_variant_reaches_write_plan_preview(SHOW_SITE_TURN_TEXT)

    async def test_reported_broken_phrase_now_reaches_write_plan_preview(self):
        # Test 2 -- the exact reported gap: "план записи ... в Bitrix".
        await self._assert_variant_reaches_write_plan_preview(PREVIEW_VARIANT_LITERAL_PLAN)

    async def test_equivalent_english_wording_reaches_write_plan_preview(self):
        # Test 3 -- equivalent English wording.
        await self._assert_variant_reaches_write_plan_preview(PREVIEW_VARIANT_ENGLISH)

    async def test_natural_wording_without_literal_write_plan_words(self):
        # Test 4 -- natural wording without the literal words "план записи".
        self.assertNotIn("план", PREVIEW_VARIANT_NO_PLAN_WORDING.casefold())
        self.assertNotIn("запис", PREVIEW_VARIANT_NO_PLAN_WORDING.casefold())
        await self._assert_variant_reaches_write_plan_preview(PREVIEW_VARIANT_NO_PLAN_WORDING)

    async def test_negative_case_actual_confirmation_is_not_treated_as_preview(self):
        # Test 5 -- an actual confirmation/write request must NOT be
        # treated as a preview: it must still perform the SAME governed
        # write #102 already proved, never a read-only preview.
        panda, artifact_service, store = _panda_with_research_backend()
        await self._select_and_price(panda, artifact_service)
        before_catalog_size = len(store.catalog("tenant-a"))

        result = await panda.respond(
            ConversationRequest(
                text=CONFIRM_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r4",
                conversation_id="conv-1",
            )
        )
        self.assertEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        write_result = result.metadata.get("bitrix_write_result") or {}
        self.assertTrue(write_result.get("mutated"))
        self.assertEqual(write_result.get("sku"), TARGET_SKU)
        self.assertEqual(len(store.catalog("tenant-a")) - before_catalog_size, 1)
        self.assertIn(TARGET_RETAIL_PRICE, result.text)


if __name__ == "__main__":
    unittest.main()
