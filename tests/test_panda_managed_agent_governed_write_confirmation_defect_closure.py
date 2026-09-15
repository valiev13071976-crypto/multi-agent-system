"""PANDA -- PRODUCTION DEFECT CLOSURE: managed-agent product preparation /
SHOW_WRITE_PLAN followed by an explicit user confirmation must reach the
EXISTING governed Bitrix write path, never a re-display of the plan.

Reproduced production failure (PR #87 follow-up):

    A fresh Bitrix write plan was shown through the managed-agent path
    (``select_product`` -> ``explain_bitrix_write_plan``), then the user
    sent exactly:

        "Подтверждаю запись этого товара в Bitrix/Aspro по показанному
        плану."

    Production logged ``MANAGED_PRODUCT_SELECTED tool=explain_bitrix_
    write_plan`` again -- the confirmation was misrouted BACK into the
    managed agent's own read-only tool selector (which exposes no write
    tool at all -- see ``managed_agent_poc``'s own module docstring) and
    the SAME preview was re-rendered. ``resolve_action_turn()`` -- where
    PR #87's ``is_explicit_bitrix_write_confirmation``/
    ``resolve_bitrix_write_confirmation``/cross-product-safety logic
    lives -- was never reached at all. No governed Bitrix write occurred.

Root cause (already proven, diagnostic-only investigation, not repeated
here): ``WorkflowPandaConversationGateway.respond()`` dispatches every
managed-agent-eligible turn (state/attachment-based eligibility, see
``managed_agent_poc.panda_bridge._is_eligible_turn``) into
``maybe_respond_via_managed_agent`` BEFORE ``resolve_action_turn()`` ever
runs, and returns early the instant the managed agent produces a result --
regardless of whether this turn's text was actually an explicit write
confirmation.

Fix (ownership model B -- ``managed_agent_poc`` remains state-pure and
NEVER touches ``ActiveTaskStore``; ``WorkflowPandaConversationGateway``
remains the sole owner of durable conversation/action state):

  PART 1 (``managed_agent_poc/panda_bridge.py``): ``maybe_respond_via_
  managed_agent``'s existing plain-dict return contract now ALSO echoes
  back the canonical product/write-request context ALREADY computed by
  the existing delegation (``_canonical_fields_and_retail_price``/
  ``build_enriched_write_request``/``serialize_write_request`` -- the
  EXACT same functions the legacy CALL_PRODUCT_ENRICHMENT/EXPLAIN_
  BITRIX_WRITE_PLAN path already uses) under
  ``metadata["bitrix_product_fields"]``/``metadata["bitrix_enrichment_
  write_request"]``/``metadata["bitrix_retail_price_preview"]``.
  ``panda_bridge.py`` still never imports/reads/writes
  ``ActiveTaskStore`` -- it only RETURNS data.

  PART 2 (``business_assistant/conversation_gateway.py``): before its
  existing managed-agent early return, the gateway now persists that
  returned context into the SAME ``ActiveTaskStore``/``ActiveTask.
  parameters`` contract the legacy FAMILY_EXCEL flow already uses (the
  EXISTING get -> mutate -> put pattern), on EVERY successful managed-
  agent product/write-plan resolution -- never only the first product in
  the conversation, so a later product switch always overwrites the
  identity/write-request pair atomically (preserving PR #87's own
  cross-product SKU guard in ``_invoke_controlled_bitrix_write``).

  PART 3 (``business_assistant/conversation_gateway.py``): the EXISTING
  canonical ``is_explicit_bitrix_write_confirmation(text)`` classifier
  (never a second phrase list, never LLM tool-selection "preference") now
  gates managed-agent dispatch itself -- an explicit confirmation turn
  skips the managed agent entirely and falls straight through to the
  EXISTING ``resolve_action_turn()``/``resolve_bitrix_write_confirmation``/
  ``CALL_CONTROLLED_BITRIX_WRITE`` chain, using the context PART 2 just
  persisted. Every other managed-agent-eligible turn (including a genuine
  "show/review the plan" question, which is NOT a confirmation) is
  unaffected.

  PART 4 (FAMILY_EXCEL compatibility): no new family was needed --
  ``resolve_bitrix_write_confirmation`` already only requires
  ``active.family == FAMILY_EXCEL``, and a managed-agent-only conversation
  never has an ``ActiveTask`` at all (it never reaches
  ``resolve_action_turn()``); the gateway's new persistence helper simply
  creates one with the SAME EXISTING ``FAMILY_EXCEL``/``EXCEL_CONTRACT``
  values ``resolve_action_turn`` itself already uses for a brand-new Excel
  conversation.

Zero real Bitrix mutation possible without this test's own explicit
approval flow: the LIVE bridge is backed by a mocked HTTP transport
(``tests.test_bitrix_live_product_create_write._RecordingTransport``);
the managed-agent subprocess itself is never invoked (``ManagedAgentPOC.
run_turn`` is replaced with a deterministic double that reproduces the
SAME tool-call shapes already proven live elsewhere in this test suite --
see ``tests.test_panda_managed_agent_enrichment_delegation._make_fake_
run_turn``'s own docstring)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from business_assistant.action_continuation import CALL_CONTROLLED_BITRIX_WRITE, FAMILY_EXCEL
from business_assistant.conversation_gateway import ConversationRequest
from integrations.production.http import BoundedHttpClient
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from product_enrichment.media_fetch import FakeImageFetcher
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport
from tests.test_panda_managed_agent_enrichment_delegation import (
    BRAND,
    CATEGORY,
    ELECTRONICS_SECTION,
    FILENAME,
    IMAGE_URL_A,
    IMAGE_URL_B,
    PRODUCT_A_EAN,
    PRODUCT_A_NAME,
    PRODUCT_A_PURCHASE_PRICE,
    PRODUCT_A_RETAIL_PRICE,
    PRODUCT_A_SKU,
    PRODUCT_B_EAN,
    PRODUCT_B_NAME,
    PRODUCT_B_PURCHASE_PRICE,
    PRODUCT_B_RETAIL_PRICE,
    PRODUCT_B_SKU,
    PRODUCTION_TEXT,
    RESEARCH_URL_A,
    RESEARCH_URL_B,
    SET_RETAIL_PRICE_800000_TEXT,
    TV_SECTION,
    TV_SECTION_ID,
    WRITE_PLAN_TEXT,
    _make_fake_run_turn,
    _no_retail_price_xlsx_bytes,
    _png_bytes,
    _raw_tool_fields,
    _scrape_fetch_handler,
    _xlsx_bytes,
)
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tools.search.fake_provider import FakeSearchProvider, fake_result

EXPLICIT_APPROVAL_SHOWN_PLAN_TEXT = "Подтверждаю запись этого товара в Bitrix/Aspro по показанному плану."


class _ManagedAgentGovernedWriteConfirmationTestBase(unittest.IsolatedAsyncioTestCase):
    """Shared LIVE-bridge + mocked-HTTP-transport + fake-subprocess wiring
    (mirrors ``tests.test_panda_managed_agent_enrichment_delegation.
    ManagedAgentShowWritePlanDelegationTests`` exactly -- same fixtures,
    never a second/duplicated harness)."""

    async def asyncSetUp(self):
        self.transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = mock.patch.object(BoundedHttpClient, "request", side_effect=self.transport)
        self.http_patch.start()
        self.bridge, _activation = _bridge_and_activation()

        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"

        self.panda, self.artifact_service = _panda(
            bitrix_bridge=self.bridge,
            search_provider=FakeSearchProvider(
                {
                    f"{BRAND} {PRODUCT_A_SKU}": [fake_result(RESEARCH_URL_A, title=f"LG {PRODUCT_A_SKU}")],
                    f"{BRAND} {PRODUCT_B_SKU}": [fake_result(RESEARCH_URL_B, title=f"LG {PRODUCT_B_SKU}")],
                }
            ),
            scrape_fetch_handler=_scrape_fetch_handler,
            media_fetcher=FakeImageFetcher({IMAGE_URL_A: _png_bytes(), IMAGE_URL_B: _png_bytes()}),
        )

    async def asyncTearDown(self):
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)
        if self._old_data_dir is None:
            os.environ.pop("PANDA_DATA_DIR", None)
        else:
            os.environ["PANDA_DATA_DIR"] = self._old_data_dir
        if self._old_flag is None:
            os.environ.pop(ENABLED_ENV_VAR, None)
        else:
            os.environ[ENABLED_ENV_VAR] = self._old_flag
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _select_product_tool_calls(self, *, name, sku, ean, purchase_price, retail_price):
        return [
            {
                "tool": "select_product",
                "output": {
                    "status": "SELECTED",
                    "matched_by": "next_unspecified",
                    **_raw_tool_fields(
                        name=name, sku=sku, ean=ean, purchase_price=purchase_price, retail_price=retail_price
                    ),
                },
            }
        ]

    def _explain_write_plan_tool_calls(self, *, name, sku, ean, purchase_price, retail_price):
        fields = _raw_tool_fields(name=name, sku=sku, ean=ean, purchase_price=purchase_price, retail_price=retail_price)
        return [
            {
                "tool": "explain_bitrix_write_plan",
                "output": {"status": "WRITE_PLAN", "would_write": dict(fields)},
            }
        ]


class ManagedProductPlanConfirmRoutesToGovernedWriteTests(_ManagedAgentGovernedWriteConfirmationTestBase):
    """REQUIRED CASE 1: MANAGED PRODUCT -> PLAN -> CONFIRM."""

    async def test_explicit_confirmation_after_managed_agent_write_plan_performs_exactly_one_governed_write(self):
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-1", filename=FILENAME, content=_xlsx_bytes()
        )
        plan = [
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": self._select_product_tool_calls(
                    name=PRODUCT_A_NAME,
                    sku=PRODUCT_A_SKU,
                    ean=PRODUCT_A_EAN,
                    purchase_price=PRODUCT_A_PURCHASE_PRICE,
                    retail_price=PRODUCT_A_RETAIL_PRICE,
                ),
                "final_output": "irrelevant-1",
            },
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": self._explain_write_plan_tool_calls(
                    name=PRODUCT_A_NAME,
                    sku=PRODUCT_A_SKU,
                    ean=PRODUCT_A_EAN,
                    purchase_price=PRODUCT_A_PURCHASE_PRICE,
                    retail_price=PRODUCT_A_RETAIL_PRICE,
                ),
                "final_output": "irrelevant-2",
            },
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            r1 = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-1",
                    attachment_refs=(artifact_id,),
                )
            )
            self.assertEqual(r1.metadata.get("managed_agent_tool"), "select_product")

            r2 = await self.panda.respond(
                ConversationRequest(
                    text=WRITE_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-1",
                )
            )
            self.assertEqual(r2.metadata.get("managed_agent_tool"), "explain_bitrix_write_plan")
            self.assertEqual(r2.metadata.get("preparation_status"), "PREPARED")

            # Existing FAMILY_EXCEL/ActiveTaskStore context was persisted
            # by the gateway (never by panda_bridge) after the write-plan
            # turn -- ownership model B, PART 2.
            task = self.panda._action_store.get(  # noqa: SLF001
                tenant_id="tenant-a", owner_id="u1", conversation_id="conv-1"
            )
            self.assertIsNotNone(task)
            self.assertEqual(task.family, FAMILY_EXCEL)
            self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), PRODUCT_A_SKU)
            self.assertEqual(
                task.parameters.get("bitrix_enrichment_write_request", {}).get("sku"), PRODUCT_A_SKU
            )

            calls_before_confirm = len(self.transport.calls)

            # THE explicit confirmation turn -- must NOT be seen by the
            # managed agent's own run_turn at all (only 2 entries exist in
            # ``plan`` above; a 3rd call would raise ``IndexError`` and
            # fail this test loudly).
            r3 = await self.panda.respond(
                ConversationRequest(
                    text=EXPLICIT_APPROVAL_SHOWN_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-3",
                    conversation_id="conv-1",
                )
            )
            calls_after_confirm = len(self.transport.calls)

        # Confirmation did NOT select ``explain_bitrix_write_plan`` again
        # -- it never entered the managed agent at all.
        self.assertNotEqual(r3.metadata.get("managed_agent_tool"), "explain_bitrix_write_plan")
        self.assertNotEqual(r3.metadata.get("action_decision"), "MANAGED_AGENT")

        # Existing governed write path was reached, exactly once.
        self.assertEqual(r3.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(r3.metadata.get("write_confirmation_event"), "WRITE_CONFIRMATION_ROUTED_TO_GOVERNED_WRITE")
        write_result = r3.metadata.get("bitrix_write_result") or {}
        self.assertTrue(write_result.get("mutated"))
        self.assertEqual(write_result.get("sku"), PRODUCT_A_SKU)

        self.assertEqual(self.transport.product_add_count, 1)
        new_calls = [m for m, _ in self.transport.calls[calls_before_confirm:calls_after_confirm]]
        self.assertEqual(new_calls.count("catalog.product.add"), 1)


class NoContextConfirmationNeverWritesTests(_ManagedAgentGovernedWriteConfirmationTestBase):
    """REQUIRED CASE 2: NO CONTEXT -> CONFIRM."""

    async def test_explicit_confirmation_without_any_prepared_product_never_writes(self):
        before = self.transport.product_add_count
        result = await self.panda.respond(
            ConversationRequest(
                text=EXPLICIT_APPROVAL_SHOWN_PLAN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="req-1",
                conversation_id="conv-no-context",
            )
        )
        self.assertNotEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertNotEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertIsNone(result.metadata.get("bitrix_write_result"))
        self.assertEqual(self.transport.product_add_count, before)
        self.assertNotIn("catalog.product.add", [m for m, _ in self.transport.calls])


class ProductSwitchStaleStateSafetyTests(_ManagedAgentGovernedWriteConfirmationTestBase):
    """REQUIRED CASE 3: PRODUCT SWITCH / STALE STATE."""

    async def test_confirmation_after_switching_products_writes_only_the_current_product(self):
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-switch", filename=FILENAME, content=_xlsx_bytes()
        )
        plan = [
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": self._explain_write_plan_tool_calls(
                    name=PRODUCT_A_NAME,
                    sku=PRODUCT_A_SKU,
                    ean=PRODUCT_A_EAN,
                    purchase_price=PRODUCT_A_PURCHASE_PRICE,
                    retail_price=PRODUCT_A_RETAIL_PRICE,
                ),
                "final_output": "irrelevant-plan-a",
            },
            {
                "current_identifier": PRODUCT_B_SKU,
                "tool_calls": self._explain_write_plan_tool_calls(
                    name=PRODUCT_B_NAME,
                    sku=PRODUCT_B_SKU,
                    ean=PRODUCT_B_EAN,
                    purchase_price=PRODUCT_B_PURCHASE_PRICE,
                    retail_price=PRODUCT_B_RETAIL_PRICE,
                ),
                "final_output": "irrelevant-plan-b",
            },
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            await self.panda.respond(
                ConversationRequest(
                    text=WRITE_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-switch",
                    attachment_refs=(artifact_id,),
                )
            )
            await self.panda.respond(
                ConversationRequest(
                    text=f"Покажи план записи для {PRODUCT_B_NAME}.",
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-switch",
                )
            )

            task = self.panda._action_store.get(  # noqa: SLF001
                tenant_id="tenant-a", owner_id="u1", conversation_id="conv-switch"
            )
            self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), PRODUCT_B_SKU)
            self.assertEqual(
                task.parameters.get("bitrix_enrichment_write_request", {}).get("sku"), PRODUCT_B_SKU
            )

            result = await self.panda.respond(
                ConversationRequest(
                    text=EXPLICIT_APPROVAL_SHOWN_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-3",
                    conversation_id="conv-switch",
                )
            )

        self.assertEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(result.metadata.get("write_confirmation_event"), "WRITE_CONFIRMATION_ROUTED_TO_GOVERNED_WRITE")
        write_result = result.metadata.get("bitrix_write_result") or {}
        self.assertEqual(write_result.get("sku"), PRODUCT_B_SKU)
        self.assertNotEqual(write_result.get("sku"), PRODUCT_A_SKU)
        self.assertEqual(self.transport.product_add_count, 1)


class CurrentPriceContextConfirmationTests(_ManagedAgentGovernedWriteConfirmationTestBase):
    """REQUIRED CASE 4: CURRENT PRICE CONTEXT -- reuses the EXISTING
    explicit retail-price refinement mechanism (``managed_agent_poc.
    panda_bridge._apply_retail_price_refinement``, already proven in
    ``tests.test_panda_managed_agent_enrichment_delegation.
    ManagedAgentExplicitRetailPriceRefinementTests``); no new price-update
    behavior is introduced here."""

    async def test_confirmation_writes_the_latest_refined_retail_price_not_the_original(self):
        artifact_id = await _register_upload(
            self.artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="conv-price",
            filename=FILENAME,
            content=_no_retail_price_xlsx_bytes(),
        )
        plan = [
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": self._explain_write_plan_tool_calls(
                    name=PRODUCT_A_NAME,
                    sku=PRODUCT_A_SKU,
                    ean=PRODUCT_A_EAN,
                    purchase_price=PRODUCT_A_PURCHASE_PRICE,
                    retail_price="",
                ),
                "final_output": "irrelevant-plan-no-price",
            },
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": self._explain_write_plan_tool_calls(
                    name=PRODUCT_A_NAME,
                    sku=PRODUCT_A_SKU,
                    ean=PRODUCT_A_EAN,
                    purchase_price=PRODUCT_A_PURCHASE_PRICE,
                    retail_price="",
                ),
                "final_output": "irrelevant-plan-refined",
            },
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            await self.panda.respond(
                ConversationRequest(
                    text=WRITE_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-price",
                    attachment_refs=(artifact_id,),
                )
            )
            r2 = await self.panda.respond(
                ConversationRequest(
                    text=SET_RETAIL_PRICE_800000_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-price",
                )
            )
            self.assertIn("800000", r2.text.replace("\u00a0", ""))

            task = self.panda._action_store.get(  # noqa: SLF001
                tenant_id="tenant-a", owner_id="u1", conversation_id="conv-price"
            )
            self.assertEqual(task.parameters.get("bitrix_retail_price_preview"), "800000")
            self.assertEqual(
                task.parameters.get("bitrix_enrichment_write_request", {}).get("retail_price"), "800000"
            )

            result = await self.panda.respond(
                ConversationRequest(
                    text=EXPLICIT_APPROVAL_SHOWN_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-3",
                    conversation_id="conv-price",
                )
            )

        self.assertEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        write_result = result.metadata.get("bitrix_write_result") or {}
        self.assertTrue(write_result.get("mutated"))
        self.assertEqual(str((write_result.get("retail_price") or {}).get("amount")), "800000")


class NormalManagedAgentRoutingUnaffectedTests(_ManagedAgentGovernedWriteConfirmationTestBase):
    """REQUIRED CASE 5: NORMAL MANAGED-AGENT ROUTING is unaffected by the
    PART 3 confirmation gate -- a genuine select/show-plan turn still goes
    through the managed agent, and neither writes Bitrix by itself."""

    async def test_select_product_and_show_write_plan_still_route_through_managed_agent(self):
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-normal", filename=FILENAME, content=_xlsx_bytes()
        )
        plan = [
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": self._select_product_tool_calls(
                    name=PRODUCT_A_NAME,
                    sku=PRODUCT_A_SKU,
                    ean=PRODUCT_A_EAN,
                    purchase_price=PRODUCT_A_PURCHASE_PRICE,
                    retail_price=PRODUCT_A_RETAIL_PRICE,
                ),
                "final_output": "irrelevant-1",
            },
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": self._explain_write_plan_tool_calls(
                    name=PRODUCT_A_NAME,
                    sku=PRODUCT_A_SKU,
                    ean=PRODUCT_A_EAN,
                    purchase_price=PRODUCT_A_PURCHASE_PRICE,
                    retail_price=PRODUCT_A_RETAIL_PRICE,
                ),
                "final_output": "irrelevant-2",
            },
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            r1 = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-normal",
                    attachment_refs=(artifact_id,),
                )
            )
            r2 = await self.panda.respond(
                ConversationRequest(
                    text=WRITE_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-normal",
                )
            )

        self.assertEqual(r1.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r1.metadata.get("managed_agent_tool"), "select_product")
        self.assertFalse(r1.metadata.get("mutated"))

        self.assertEqual(r2.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r2.metadata.get("managed_agent_tool"), "explain_bitrix_write_plan")
        self.assertFalse(r2.metadata.get("mutated"))

        self.assertNotIn("catalog.product.add", [m for m, _ in self.transport.calls])
        self.assertEqual(self.transport.product_add_count, 0)


if __name__ == "__main__":
    unittest.main()
