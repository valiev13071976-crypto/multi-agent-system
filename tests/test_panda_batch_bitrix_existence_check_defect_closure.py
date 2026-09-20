"""PRODUCT-FIRST DEFECT CLOSURE (batch Bitrix existence check): a natural,
ordinary-language request to check an ALREADY UPLOADED price list against
the EXISTING, already-connected Bitrix integration -- "which rows already
exist, which are new, which are ambiguous" -- must use the SAME
single-product duplicate/read logic (``BitrixProductBridge.plan_sync``)
the single-product write-plan preview already uses, applied once per row.

Reproduced production defect:

    upload TCL.xlsx (34 products)
    -> "Подготовь все товары из этого прайса для загрузки на сайт, но пока
       ничего в Bitrix не записывай. Покажи, сколько товаров будет создано,
       какие уже существуют и какие позиции требуют уточнения."
    -> Panda: "не могу обратиться к Bitrix... предоставьте экспорт SKU,
       критерий уникальности..."
    -> "Используй уже подключенный Bitrix/Aspro и сам проверь все 34
       позиции по артикулу/SKU. Ничего не записывай."
    -> Panda: "Не могу обратиться к Bitrix/Aspro изнутри этого чата: у
       меня нет инструмента для проверки наличия товаров."

ROOT CAUSE: there was no dispatch path from a "check the WHOLE dataset
against Bitrix" business request to ``BitrixProductBridge`` at all. The
single-product flow (``EXPLAIN_BITRIX_WRITE_PLAN``/
``CALL_CONTROLLED_BITRIX_WRITE``) only ever calls ``plan_sync`` for ONE
already-selected product's SKU; a request naming no single product fell
through to generic ``FAMILY_EXCEL`` spreadsheet analysis (or the LLM
fallback), neither of which has ever heard of ``BitrixProductBridge``, so
the model invented the "no Bitrix tool / need an export" reply.

MINIMAL FIX (reuses existing code, no new Bitrix client/agent/router/
store):

1. ``business_assistant.action_continuation.is_batch_bitrix_existence_
   check_request`` -- a new, composable-semantic-stem predicate (ask verb +
   either an "already exists/will be created" outcome phrase, or a "whole
   dataset" scope phrase combined with a Bitrix/site/upload target). Never
   requires a single product to already be selected (unlike
   ``is_read_only_write_preview_ask``).
2. ``resolve_batch_bitrix_existence_check`` dispatches to the new
   ``CHECK_BITRIX_EXISTENCE_BATCH`` action once an active ``FAMILY_EXCEL``
   dataset exists.
3. ``DataIntelligenceService.canonical_identity_rows`` (new, read-only) --
   the SAME per-row ``product_fields`` projection ``_row_lookup_result``
   already builds for a single row, exposed for EVERY row via a new
   ``canonical_identity_rows`` operation on the EXISTING
   ``data.excel_assistant`` tool.
4. ``WorkflowPandaConversationGateway._check_bitrix_existence_batch`` reads
   those rows through the existing ``ToolGateway``, then calls the
   EXISTING, unchanged ``BitrixProductBridge.plan_sync`` once per row (the
   SAME call ``_explain_bitrix_write_plan`` already makes for one product)
   and classifies NEW / EXISTING / AMBIGUOUS / INVALID deterministically.
   Zero ``catalog.product.add``/``update`` calls anywhere in this path.

This module proves the fix end to end through a REAL
``WorkflowPandaConversationGateway`` wired to a REAL (fixture-mode)
``BitrixProductBridge``/``BitrixCatalogStore`` -- the SAME fixture
machinery the single-product acceptance tests already use -- with zero
live network/Bitrix mutation calls.
"""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import (
    CHECK_BITRIX_EXISTENCE_BATCH,
    is_batch_bitrix_existence_check_request,
    is_explicit_bitrix_write_confirmation,
)
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

TENANT = "tenant-a"
OWNER = "user-a"
CONV = "conv-1"
FILENAME = "TCL_price_list.xlsx"

# Seeded in the shared fixture catalog (``integrations/bitrix/catalog.py``).
EXISTING_SKU = "SKU-X100"
EXISTING_TITLE_IN_PRICE_LIST = "TCL 55-inch OLED (price-list title)"
AMBIGUOUS_SKU = "SKU-AMBIG"
NEW_SKU = "TCL-NEW-77Q10K"
NEW_TITLE = "TCL 77Q10K QLED TV"

MANDATORY_ACCEPTANCE_TURN_TEXT = (
    "Проверь весь прайс перед загрузкой на сайт. Покажи, какие товары уже есть в Bitrix, "
    "каких нет и где есть неоднозначность. Ничего не записывай."
)
FIRST_REPORTED_TURN_TEXT = (
    "Подготовь все товары из этого прайса для загрузки на сайт, но пока ничего в Bitrix "
    "не записывай. Покажи, сколько товаров будет создано, какие уже существуют и какие "
    "позиции требуют уточнения."
)
SECOND_REPORTED_TURN_TEXT = (
    "Используй уже подключенный Bitrix/Aspro и сам проверь все 34 позиции по артикулу/SKU. "
    "Ничего не записывай."
)

FORBIDDEN_ASSISTANT_PHRASES = (
    "не могу обратиться",
    "нет инструмента",
    "предоставьте",
    "экспорт",
    "критерий уникальности",
)


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "purchase_price"])
    ws.append([EXISTING_SKU, EXISTING_TITLE_IN_PRICE_LIST, "TV", "TCL", "38000"])
    ws.append([NEW_SKU, NEW_TITLE, "TV", "TCL", "42000"])
    ws.append([AMBIGUOUS_SKU, "TCL Accessory (ambiguous)", "Accessories", "TCL", "500"])
    # A malformed row: no SKU/article at all.
    ws.append(["", "Unknown row with no article", "TV", "TCL", "1000"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _bitrix_bridge_and_store() -> tuple[BitrixProductBridge, BitrixCatalogStore]:
    store = BitrixCatalogStore()
    # Force a genuine Bitrix-side duplicate for AMBIGUOUS_SKU (same pattern
    # ``tests/test_block5_6_bitrix_aspro_integration.py`` already uses).
    store.create_product(tenant_id=TENANT, payload={"name": "Dup", "article": AMBIGUOUS_SKU, "price": "10"})
    activation = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    activation._adapters["bitrix"] = adapter  # noqa: SLF001
    ref = activation.put_secret_ref(tenant_id=TENANT, secret_ref="secret:bitrix-tenant-a", value="tok")
    conn = activation.configure_connection(
        tenant_id=TENANT, provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
    )
    activation.verify_connection(tenant_id=TENANT, connection_id=conn.connection_id)
    activation.activate_connection(tenant_id=TENANT, connection_id=conn.connection_id)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, store


def _panda() -> tuple[WorkflowPandaConversationGateway, ArtifactService, BitrixCatalogStore]:
    svc = DataIntelligenceService(InMemoryDatasetStore())
    artifact_service = ArtifactService(store=InMemoryArtifactStore())
    svc.artifact_service = artifact_service
    registry = ToolRegistry()
    register_platform_tools(registry, data_intelligence=svc)
    gateway = ToolGateway(registry=registry, register_search=False)
    bridge, store = _bitrix_bridge_and_store()
    panda = WorkflowPandaConversationGateway(
        workflow_engine=object(),
        run_router=object(),
        context_manager=object(),
        tool_gateway=gateway,
        artifact_service=artifact_service,
        bitrix_product_bridge=bridge,
    )
    return panda, artifact_service, store


async def _register_upload(artifact_service: ArtifactService) -> str:
    rec = artifact_service.register_upload(
        tenant_id=TENANT, owner_id=OWNER, filename=FILENAME, content=_xlsx_bytes()
    )
    artifact_service.attach_to_conversation(tenant_id=TENANT, artifact_id=rec.artifact_id, conversation_id=CONV)
    return rec.artifact_id


class BatchBitrixExistenceCheckPredicateUnitTests(unittest.TestCase):
    """Unit-level coverage: ordinary business phrasing, not a growing
    phrase list, and never confused with an actual write confirmation or
    the unrelated in-spreadsheet duplicate finder."""

    def test_mandatory_acceptance_phrase_matches(self):
        self.assertTrue(is_batch_bitrix_existence_check_request(MANDATORY_ACCEPTANCE_TURN_TEXT))

    def test_first_reported_production_phrase_matches(self):
        self.assertTrue(is_batch_bitrix_existence_check_request(FIRST_REPORTED_TURN_TEXT))

    def test_second_reported_production_phrase_matches(self):
        self.assertTrue(is_batch_bitrix_existence_check_request(SECOND_REPORTED_TURN_TEXT))

    def test_english_equivalent_matches(self):
        self.assertTrue(
            is_batch_bitrix_existence_check_request(
                "Check the whole price list against Bitrix before uploading it to the site."
            )
        )

    def test_write_confirmation_never_matches(self):
        text = "Подтверждаю: создай этот товар в Bitrix."
        self.assertTrue(is_explicit_bitrix_write_confirmation(text))
        self.assertFalse(is_batch_bitrix_existence_check_request(text))

    def test_unrelated_in_spreadsheet_duplicate_question_does_not_match(self):
        # "дублируются в файле" (rows duplicated WITHIN the spreadsheet)
        # is a different, EXISTING, unrelated feature
        # (``DataIntelligenceService.duplicates``) and must be unaffected.
        self.assertFalse(is_batch_bitrix_existence_check_request("Какие товары дублируются в файле?"))

    def test_bare_analysis_question_does_not_match(self):
        self.assertFalse(is_batch_bitrix_existence_check_request("Сколько строк и столбцов в этом файле?"))


class BatchBitrixExistenceCheckAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """MANDATORY ACCEPTANCE: real conversational journey through
    ``WorkflowPandaConversationGateway`` -- upload -> analyze -> ordinary-
    language batch check -> deterministic NEW/EXISTING/AMBIGUOUS/INVALID
    classification against the REAL (fixture) connected Bitrix, zero
    writes."""

    async def _upload_and_analyze(self, panda, artifact_service):
        ref = await _register_upload(artifact_service)
        upload_result = await panda.respond(
            ConversationRequest(
                text="Вот прайс-лист поставщика TCL, посмотри, что там есть.",
                tenant_id=TENANT,
                user_id=OWNER,
                request_id="r1",
                conversation_id=CONV,
                attachment_refs=(ref,),
            )
        )
        self.assertTrue(upload_result.text)
        analyze_result = await panda.respond(
            ConversationRequest(
                text="Проанализируй этот прайс.",
                tenant_id=TENANT,
                user_id=OWNER,
                request_id="r2",
                conversation_id=CONV,
            )
        )
        self.assertTrue(analyze_result.text)

    async def test_mandatory_natural_journey_batch_existence_check(self):
        panda, artifact_service, store = _panda()
        await self._upload_and_analyze(panda, artifact_service)
        before_catalog_size = len(store.catalog(TENANT))

        result = await panda.respond(
            ConversationRequest(
                text=MANDATORY_ACCEPTANCE_TURN_TEXT,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id="r3",
                conversation_id=CONV,
            )
        )

        self.assertEqual(result.metadata.get("action_decision"), CHECK_BITRIX_EXISTENCE_BATCH)
        blob = result.text.casefold()
        for forbidden in FORBIDDEN_ASSISTANT_PHRASES:
            self.assertNotIn(forbidden, blob, f"reply must never say {forbidden!r}: {result.text!r}")

        check = result.metadata.get("bitrix_existence_check") or {}
        self.assertEqual(check.get("total"), 4)

        new_skus = {row["sku"] for row in check.get("new") or []}
        existing_skus = {row["sku"] for row in check.get("existing") or []}
        ambiguous_skus = {row["sku"] for row in check.get("ambiguous") or []}
        invalid_reasons = [row["reason"] for row in check.get("invalid") or []]

        self.assertEqual(new_skus, {NEW_SKU})
        self.assertEqual(existing_skus, {EXISTING_SKU})
        self.assertEqual(ambiguous_skus, {AMBIGUOUS_SKU})
        self.assertEqual(len(check.get("invalid") or []), 1)
        self.assertIn("missing_sku_or_title", invalid_reasons)

        # Bitrix product id surfaced for the EXISTING row.
        existing_row = next(row for row in check["existing"] if row["sku"] == EXISTING_SKU)
        self.assertTrue(existing_row.get("bitrix_id"))

        # Deterministic text also carries the counts and identities.
        self.assertIn("ВСЕГО ТОВАРОВ: 4", result.text)
        self.assertIn(NEW_SKU, result.text)
        self.assertIn(EXISTING_SKU, result.text)
        self.assertIn(AMBIGUOUS_SKU, result.text)
        self.assertIn("Ничего в Bitrix не записано", result.text)

        # ZERO writes: Bitrix catalog is completely unchanged.
        self.assertEqual(len(store.catalog(TENANT)), before_catalog_size)
        self.assertEqual(result.metadata.get("mutated"), False)

    async def test_second_reported_phrase_reaches_the_same_batch_check(self):
        # "Используй уже подключенный Bitrix/Aspro и сам проверь все 34
        # позиции по артикулу/SKU. Ничего не записывай." -- the EXACT
        # follow-up that used to produce "Не могу обратиться к
        # Bitrix/Aspro..." -- must now reach the same deterministic check.
        panda, artifact_service, store = _panda()
        await self._upload_and_analyze(panda, artifact_service)
        before_catalog_size = len(store.catalog(TENANT))

        result = await panda.respond(
            ConversationRequest(
                text=SECOND_REPORTED_TURN_TEXT,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id="r3",
                conversation_id=CONV,
            )
        )
        self.assertEqual(result.metadata.get("action_decision"), CHECK_BITRIX_EXISTENCE_BATCH)
        blob = result.text.casefold()
        for forbidden in FORBIDDEN_ASSISTANT_PHRASES:
            self.assertNotIn(forbidden, blob)
        self.assertEqual((result.metadata.get("bitrix_existence_check") or {}).get("total"), 4)
        self.assertEqual(len(store.catalog(TENANT)), before_catalog_size)

    async def test_no_export_or_uniqueness_rule_requested(self):
        panda, artifact_service, _store = _panda()
        await self._upload_and_analyze(panda, artifact_service)
        result = await panda.respond(
            ConversationRequest(
                text=FIRST_REPORTED_TURN_TEXT,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id="r3",
                conversation_id=CONV,
            )
        )
        self.assertEqual(result.metadata.get("action_decision"), CHECK_BITRIX_EXISTENCE_BATCH)
        blob = result.text.casefold()
        self.assertNotIn("экспорт", blob)
        self.assertNotIn("критерий уникальности", blob)
        self.assertNotIn("уникальност", blob)

    async def test_missing_dataset_fails_closed_without_bitrix_call(self):
        # No upload at all yet -- the batch predicate matches the text,
        # but there is no dataset to check, so this must fail closed with
        # a clear message rather than ever touching Bitrix.
        panda, _artifact_service, store = _panda()
        before_catalog_size = len(store.catalog(TENANT))
        result = await panda.respond(
            ConversationRequest(
                text=MANDATORY_ACCEPTANCE_TURN_TEXT,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id="r1",
                conversation_id=CONV,
            )
        )
        self.assertIn("прайс-лист", result.text.casefold())
        self.assertEqual(len(store.catalog(TENANT)), before_catalog_size)


if __name__ == "__main__":
    unittest.main()
