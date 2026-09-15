"""Write-plan confirmation state-binding defect closure.

STATE / PRODUCT BINDING (investigated, not changed): the "prepared product
awaiting confirmation" is held on the SAME existing conversational state
mechanism the codebase already uses across turns --
``business_assistant.action_continuation.ActiveTask.parameters`` (durable
via ``SqliteActiveTaskStore`` in production) -- under the
``bitrix_product_fields`` / ``bitrix_enrichment_write_request`` keys. There
is no separate "pending write plan" object, no new store, and this closure
introduces none: it only adds a consistency check between the TWO existing
identity-carrying keys already on that same ``ActiveTask``.

CROSS-PRODUCT SAFETY (the actual defect): a normal product switch already
clears ``bitrix_enrichment_write_request`` the instant a NEW row is
selected (``ROW_FOUND`` handling in
``WorkflowPandaConversationGateway._invoke_tool``), which
``tests.test_panda_product_enrichment_conversational.
ProductEnrichmentConversationalFollowUpTests.
test_switching_to_a_different_product_discards_stale_enrichment`` already
covers. But nothing previously re-validated that invariant AT THE WRITE
BOUNDARY itself (``WorkflowPandaConversationGateway.
_invoke_controlled_bitrix_write``) -- if the active task's product context
were ever replaced/invalidated by some OTHER path without that same
cleanup running first, a stale ``bitrix_enrichment_write_request`` for one
product could silently be written under a confirmation the user believed
applied to a DIFFERENT (current) product. This closure adds that
belt-and-suspenders identity check directly at the write boundary, using
ONLY the existing canonical SKU identity already present in both keys --
no new state store, no new TTL/expiry (EXISTING EXPIRY CONTRACT: NONE --
there was and is no time/turn-based expiry anywhere in this path).

CONFIRMATION SEMANTICS: the existing deterministic phrase-stem classifier
(``is_explicit_bitrix_write_confirmation`` -- approval marker + Bitrix/
Aspro target + create/write verb, ALL required in the same message) is the
canonical mechanism; this closure only widens its TARGET signal to also
recognize an explicit reference to "the shown plan" (e.g. "по показанному
плану"/"shown plan") as equivalent to naming Bitrix/Aspro literally,
covering confirmations that refer back to an already-displayed plan
instead of repeating the integration's name. It is not a new ad-hoc phrase
dictionary: it reuses the SAME stem-matching primitive
(``_has_stem``/``_norm``) as every other marker group in that function."""

from __future__ import annotations

import unittest

from business_assistant.action_continuation import (
    CALL_CONTROLLED_BITRIX_WRITE,
    is_explicit_bitrix_write_confirmation,
)
from business_assistant.conversation_gateway import ConversationRequest
from tests.test_panda_product_enrichment_conversational import (
    ENRICHMENT_TURN_TEXT,
    PREVIEW_TURN_TEXT,
    TARGET_SKU,
    USER_RETAIL_PRICE_RUB,
    _bitrix_bridge,
    _panda,
    _price_list_bytes,
    _register_upload,
)

STALE_OTHER_SKU = "99ZZ00000ZZ.STALE"

EXPLICIT_APPROVAL_TEXT = (
    f"Подтверждаю: создай этот товар в Bitrix. Розничная цена {USER_RETAIL_PRICE_RUB} \u20bd."
)


class ConfirmationRoutingRecognizesShownPlanReferenceTests(unittest.TestCase):
    """Confirmation-semantics section: unambiguous confirmation intent must
    be recognized by the EXISTING canonical stem-based mechanism, not one
    hardcoded sentence."""

    def test_confirmation_naming_bitrix_and_the_shown_plan_together_routes(self):
        self.assertTrue(
            is_explicit_bitrix_write_confirmation(
                "Подтверждаю запись этого товара в Bitrix/Aspro по показанному плану."
            )
        )

    def test_confirmation_referring_only_to_the_shown_plan_without_repeating_bitrix_routes(self):
        # This is the exact focused gap: the message never repeats
        # "Bitrix"/"Aspro" -- it only refers back to the plan already
        # shown to the user -- so the confirmation-target signal must be
        # satisfied via the shown-plan reference instead.
        self.assertTrue(is_explicit_bitrix_write_confirmation("Да, подтверждаю запись по показанному плану."))

    def test_confirmation_with_explicit_bitrix_write_verb_still_routes(self):
        self.assertTrue(is_explicit_bitrix_write_confirmation("Подтверждаю, записывай в Bitrix как показано."))

    def test_ambiguous_standalone_replies_remain_rejected(self):
        # The existing safety contract is untouched: a bare continuation
        # can never be inferred as approval, with or without a "plan"
        # word nearby.
        for ambiguous in ("Да", "ок", "продолжай", "давай", "хорошо, по плану"):
            with self.subTest(ambiguous=ambiguous):
                self.assertFalse(is_explicit_bitrix_write_confirmation(ambiguous))

    def test_shown_plan_reference_alone_without_approval_marker_does_not_route(self):
        # "shown plan" is only an alternative TARGET signal -- the approval
        # marker and create/write verb are still both independently
        # required, exactly as before.
        self.assertFalse(is_explicit_bitrix_write_confirmation("Запиши по показанному плану."))


class CrossProductWriteConfirmationSafetyTests(unittest.IsolatedAsyncioTestCase):
    """CRITICAL CROSS-PRODUCT SAFETY section: a confirmation must never
    silently apply a stale/mismatched write-plan identity."""

    async def _prepare_enriched_task(self):
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
        return panda, store

    async def test_mismatched_enrichment_and_product_identity_is_rejected_not_silently_written(self):
        panda, store = await self._prepare_enriched_task()

        # Simulate the active task's canonical product identity being
        # replaced/invalidated by some path OTHER than the normal
        # ``ROW_FOUND`` handler (which would otherwise have already
        # cleared ``bitrix_enrichment_write_request`` itself) -- e.g. a
        # future/alternate code path that mutates ``bitrix_product_fields``
        # without running that same cleanup. The enriched write request
        # still names the ORIGINAL product's sku.
        task = panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")
        self.assertIsNotNone(task)
        self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), TARGET_SKU)
        self.assertEqual(
            task.parameters.get("bitrix_enrichment_write_request", {}).get("sku"), TARGET_SKU
        )
        mismatched_fields = dict(task.parameters["bitrix_product_fields"])
        mismatched_fields["sku"] = STALE_OTHER_SKU
        task.parameters["bitrix_product_fields"] = mismatched_fields
        panda._action_store.put(task)

        before = len(store.catalog("tenant-a"))
        result = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c1",
            )
        )

        self.assertEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(
            result.metadata.get("write_confirmation_event"), "WRITE_CONFIRMATION_REJECTED_INSUFFICIENT_CONTEXT"
        )
        self.assertIsNone(result.metadata.get("bitrix_write_result"))
        # Neither the stale product NOR the mismatched-context product was
        # silently written.
        self.assertEqual(len(store.catalog("tenant-a")), before)

        # Self-healing: the stale enrichment state was cleared, so a fresh
        # confirmation attempt now falls back to the (mismatched-but-only)
        # canonical ``bitrix_product_fields`` and writes THAT identity
        # cleanly instead of repeating the same rejection forever.
        healed_task = panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")
        self.assertNotIn("bitrix_enrichment_write_request", healed_task.parameters)

        retry = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r4",
                conversation_id="c1",
            )
        )
        self.assertEqual(retry.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(retry.metadata.get("write_confirmation_event"), "WRITE_CONFIRMATION_ROUTED_TO_GOVERNED_WRITE")
        retry_result = retry.metadata.get("bitrix_write_result") or {}
        self.assertEqual(retry_result.get("sku"), STALE_OTHER_SKU)

    async def test_consistent_context_routes_to_governed_write_and_emits_the_routed_event(self):
        panda, store = await self._prepare_enriched_task()
        before = len(store.catalog("tenant-a"))

        result = await panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c1",
            )
        )

        self.assertEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(result.metadata.get("write_confirmation_event"), "WRITE_CONFIRMATION_ROUTED_TO_GOVERNED_WRITE")
        write_result = result.metadata.get("bitrix_write_result") or {}
        self.assertEqual(write_result.get("sku"), TARGET_SKU)
        self.assertGreater(len(store.catalog("tenant-a")), before)


if __name__ == "__main__":
    unittest.main()
