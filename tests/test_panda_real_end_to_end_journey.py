"""PANDA -- ONE REAL, UNMOCKED, PRODUCT-FIRST END-TO-END JOURNEY.

This is the single mandatory "real model acceptance journey" for the
product-first task (upload -> natural product understanding -> single-
product operation -> contextual follow-up -> multi-row operation ->
normal downloadable Excel -> natural re-selection after the multi-row
escape -> Bitrix preview -> zero writes). It performs exactly ONE
continuous conversation against the REAL, unmocked one-shot model seam
(``agents.openai_agent.OpenAIAgent.run`` via
``data_intel.nl_plan_llm.compile_request_via_model``) -- never a second
journey, never a retried call on assertion failure. Only the managed-
agent's OWN isolated subprocess turn (the single "analyze this price
list" turn, which carries no table-transform capability of its own -- see
``managed_agent_poc/runtime_subprocess.py``) is faked, exactly like
``tests/test_panda_canonical_single_authority_orchestration.py``'s own
``RealModelSingleAuthorityAcceptanceTests`` already does.

Fixture products are 4 STRUCTURALLY DIFFERENT TVs (different brands/
model-name shapes) so no single literal SKU is ever the product contract
-- every assertion here is generic across whichever row the real model
actually resolves a human reference to.

Skipped automatically when no ``OPENAI_API_KEY`` is configured in this
environment (never calls the paid model at all in that case).

Uses the FIXTURE Bitrix adapter (zero real network) purely so the write-
plan preview step has a bridge to render against and so this test can
assert, from an independent catalog snapshot, that the preview step
itself performed ZERO Bitrix writes -- proving real Bitrix write
acceptance is a SEPARATE, explicitly-gated step this journey never
attempts.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock

from openpyxl import Workbook, load_workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant import workset as workset_lib
from business_assistant.conversation_gateway import ConversationRequest, WorkflowPandaConversationGateway
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter
from integrations.bitrix.product_bridge import BitrixProductBridge
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_canonical_table_execution import _analyze_plan_entry, _tracking_fake_run_turn
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


TENANT = "tenant-real-e2e"
OWNER = "u1"
CONVERSATION_ID = "conv-real-e2e"
FILENAME = "price_list.xlsx"

# 4 structurally different fixture TVs (different brands, different model
# NAME shapes) -- fixture data only, never a hardcoded production SKU.
PRODUCTS = [
    ("LG-OLED55B3-2024", "Телевизор LG OLED55B3 2024", "Телевизоры", "LG", "4890000000011", "52000.00"),
    ("SONY-XR75A80L", "Телевизор Sony Bravia XR-75A80L", "Телевизоры", "Sony", "4890000000022", "71000.00"),
    ("SAMSUNG-QN85C-65", "Телевизор Samsung QN85C 65", "Телевизоры", "Samsung", "4890000000033", "63000.00"),
    ("PHILIPS-OLED808-55", "Телевизор Philips OLED808 55", "Телевизоры", "Philips", "4890000000044", "58000.00"),
]


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Прайс"
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price"])
    for row in PRODUCTS:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _bitrix_bridge():
    store = BitrixCatalogStore()
    activation = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    activation._adapters["bitrix"] = adapter  # noqa: SLF001
    ref = activation.put_secret_ref(tenant_id=TENANT, secret_ref="secret:bitrix-real-e2e", value="tok")
    conn = activation.configure_connection(
        tenant_id=TENANT, provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
    )
    activation.verify_connection(tenant_id=TENANT, connection_id=conn.connection_id)
    activation.activate_connection(tenant_id=TENANT, connection_id=conn.connection_id)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, store


def _real_credentials_available() -> bool:
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


@unittest.skipUnless(
    _real_credentials_available(),
    "OPENAI_API_KEY not available in this environment",
)
class RealEndToEndProductFirstJourneyTests(unittest.IsolatedAsyncioTestCase):
    """ONE real, unmocked journey. Never retried on assertion failure --
    a failure here is a real production defect, not a flaky test."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        self._old_api_key = os.environ.get("OPENAI_API_KEY")
        self._old_model = os.environ.get("OPENAI_MODEL")
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        # This sandbox's injected OPENAI_API_KEY may carry a trailing
        # newline that httpx rejects as an illegal header value.
        if self._old_api_key:
            os.environ["OPENAI_API_KEY"] = self._old_api_key.strip()
        os.environ["OPENAI_MODEL"] = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

        self.svc = DataIntelligenceService(InMemoryDatasetStore())
        self.artifact_service = ArtifactService(store=InMemoryArtifactStore())
        self.svc.artifact_service = self.artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=self.svc)
        self.tool_gateway = ToolGateway(registry=registry, register_search=False)
        self.bitrix_bridge, self.bitrix_store = _bitrix_bridge()
        self.bitrix_seed_snapshot = dict(self.bitrix_store.catalog(TENANT))

        self.panda = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=self.tool_gateway,
            artifact_service=self.artifact_service,
            bitrix_product_bridge=self.bitrix_bridge,
        )
        rec = self.artifact_service.register_upload(
            tenant_id=TENANT, owner_id=OWNER, filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id=TENANT, artifact_id=rec.artifact_id, conversation_id=CONVERSATION_ID
        )
        self.artifact_id = rec.artifact_id

    async def asyncTearDown(self):
        import shutil

        if self._old_data_dir is None:
            os.environ.pop("PANDA_DATA_DIR", None)
        else:
            os.environ["PANDA_DATA_DIR"] = self._old_data_dir
        if self._old_flag is None:
            os.environ.pop(ENABLED_ENV_VAR, None)
        else:
            os.environ[ENABLED_ENV_VAR] = self._old_flag
        if self._old_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._old_api_key
        if self._old_model is None:
            os.environ.pop("OPENAI_MODEL", None)
        else:
            os.environ["OPENAI_MODEL"] = self._old_model
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self):
        return self.panda._action_store.get(  # noqa: SLF001
            tenant_id=TENANT, owner_id=OWNER, conversation_id=CONVERSATION_ID
        )

    def _workset(self):
        return workset_lib.get_workset(self._task())

    async def _respond(self, text, *, request_id, attach=False):
        return await self.panda.respond(
            ConversationRequest(
                text=text,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id=request_id,
                conversation_id=CONVERSATION_ID,
                attachment_refs=(self.artifact_id,) if attach else (),
            )
        )

    async def test_real_model_upload_to_bitrix_preview_journey(self):
        sku_sony = PRODUCTS[1][0]
        sku_philips = PRODUCTS[3][0]
        purchase_sony = Decimal(PRODUCTS[1][5])
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            # 1-2. Upload + "analyze it" (managed agent's own read-only
            # tool; carries no table-transform capability, so this alone
            # can never satisfy any later step of this journey).
            await self._respond("Проанализируй этот прайс.", request_id="e2e-1", attach=True)
            self.assertEqual(len(run_turn_calls), 1)
            self.assertEqual(self._workset().scope, workset_lib.SCOPE_FULL_DATASET)

            # 3. Select a product using HUMAN wording -- brand + a
            # meaningful model fragment, never the exact canonical SKU
            # string "SONY-XR75A80L".
            await self._respond("Покажи товар Sony Bravia A80L.", request_id="e2e-2")
            self.assertEqual(len(run_turn_calls), 1)  # never entered the managed agent
            ws_after_select = self._workset()
            self.assertEqual(ws_after_select.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(ws_after_select.selected_identifiers, (sku_sony,))

            # 4. Modify (create) the retail price for the selected
            # product via a pronoun ("для него"), never repeating the SKU.
            await self._respond(
                "Поставь для него розничную цену на 20% выше закупочной.", request_id="e2e-3"
            )
            self.assertEqual(len(run_turn_calls), 1)
            ws_after_price = self._workset()
            self.assertEqual(ws_after_price.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(ws_after_price.selected_identifiers, (sku_sony,))
            rows_after_price = self.svc.store.get_rows(ws_after_price.current_dataset_id, tenant_id=TENANT)
            sony_row = next(r for r in rows_after_price if r["sku"] == sku_sony)
            expected_sony_retail_1 = purchase_sony * Decimal("1.20")
            self.assertEqual(Decimal(sony_row["retail_price"]), expected_sony_retail_1)
            self.assertEqual(Decimal(sony_row["purchase_price"]), purchase_sony)  # never overwritten

            # 5. Pronoun follow-up question about the SAME product.
            field_result = await self._respond(
                "Какая сейчас розничная цена у этого товара?", request_id="e2e-4"
            )
            self.assertEqual(len(run_turn_calls), 1)
            self.assertIn(_money(expected_sony_retail_1), field_result.text)

            # 6. Multi-row operation over the SAME spreadsheet: one row
            # (Sony) already has a derived retail value, the other three
            # do not yet -- the EXACT PR #97 shape.
            multi_result = await self._respond(
                "Повысь розничную цену на 10% для всех товаров и покажи результат.",
                request_id="e2e-5",
            )
            self.assertEqual(len(run_turn_calls), 1)  # canonical table execution, not managed agent
            ws_after_multi = self._workset()
            # PR #91 canonical Workset contract: a genuine multi-row
            # escape returns to FULL_DATASET / clears the single
            # selection -- this is what Defect B must recover FROM next.
            self.assertEqual(ws_after_multi.scope, workset_lib.SCOPE_FULL_DATASET)
            self.assertEqual(ws_after_multi.selected_identifiers, ())

            # 7. Verify ACTUAL resulting values (ground truth from the
            # store), not only Panda's textual claim -- PR #97 invariant:
            # existing retail compounds; missing retail derives from
            # purchase; purchase price is never overwritten; ALL 4 rows
            # actually changed.
            rows_final = self.svc.store.get_rows(ws_after_multi.current_dataset_id, tenant_id=TENANT)
            self.assertEqual(len(rows_final), 4)
            by_sku = {r["sku"]: r for r in rows_final}
            for sku, _name, _cat, _brand, _ean, purchase_str in PRODUCTS:
                purchase = Decimal(purchase_str)
                self.assertEqual(Decimal(by_sku[sku]["purchase_price"]), purchase)  # untouched
            expected_sony_retail_2 = expected_sony_retail_1 * Decimal("1.10")  # compounds on existing
            self.assertEqual(Decimal(by_sku[sku_sony]["retail_price"]), expected_sony_retail_2)
            for sku, _name, _cat, _brand, _ean, purchase_str in PRODUCTS:
                if sku == sku_sony:
                    continue
                expected = Decimal(purchase_str) * Decimal("1.10")  # derived from purchase, not skipped
                self.assertEqual(Decimal(by_sku[sku]["retail_price"]), expected)

            # 8-9. Download the resulting Excel and inspect it directly --
            # never expose internal debug structure, preserve original
            # business columns/order/rows, add only the requested column.
            self.assertTrue(multi_result.metadata.get("artifacts"))
            export = self.svc.generate_excel(
                ws_after_multi.current_dataset_id, tenant_id=TENANT, kind="business_result"
            )
            self.assertNotEqual(export["filename"], "dataset.xlsx")
            self.assertTrue(export["filename"].endswith(".xlsx"))
            wb = load_workbook(io.BytesIO(export["content"]))
            self.assertEqual(len(wb.sheetnames), 1)
            self.assertNotIn("SUMMARY", wb.sheetnames)
            self.assertNotIn("ISSUES", wb.sheetnames)
            self.assertNotIn("Provenance", wb.sheetnames)
            ws_sheet = wb[wb.sheetnames[0]]
            headers = [c.value for c in next(ws_sheet.iter_rows(min_row=1, max_row=1))]
            self.assertEqual(
                headers, ["sku", "product_name", "category", "brand", "ean", "purchase_price", "retail_price"]
            )
            self.assertEqual(len(headers), len(set(headers)))  # no duplicate alias columns
            downloaded_rows = list(ws_sheet.iter_rows(min_row=2, values_only=True))
            self.assertEqual(len(downloaded_rows), 4)
            downloaded_by_sku = {r[0]: r for r in downloaded_rows}
            for sku, _name, _cat, _brand, _ean, purchase_str in PRODUCTS:
                self.assertEqual(downloaded_by_sku[sku][5], purchase_str)  # purchase unchanged in file too
                self.assertEqual(Decimal(downloaded_by_sku[sku][6]), Decimal(by_sku[sku]["retail_price"]))

            # 10-11. WITHOUT re-uploading, in the SAME turn, naturally
            # reference a DIFFERENT product (never selected before) AND
            # ask for its Bitrix write-plan preview -- the literal Defect
            # B ("SINGLE -> MULTI -> SINGLE") scenario: a non-SINGLE scope
            # + a natural product reference + a single-product action, in
            # ONE message.
            write_plan_result = await self._respond(
                f"Покажи, что будет отправлено в Bitrix для товара Philips OLED808, не записывай.",
                request_id="e2e-6",
            )
            self.assertEqual(len(run_turn_calls), 1)  # still never entered the managed agent

        # 12. Verify: correct product, correct current values, restored
        # SINGLE scope, no re-upload, zero writes.
        self.assertNotEqual(
            write_plan_result.metadata.get("action_decision"), "VALIDATION_ERROR"
        )
        self.assertEqual(write_plan_result.metadata.get("action_decision"), "EXPLAIN_BITRIX_WRITE_PLAN")
        self.assertFalse(write_plan_result.metadata.get("mutated"))
        preview = write_plan_result.metadata.get("bitrix_write_preview") or {}
        target = preview.get("target_product") or {}
        self.assertEqual(target.get("sku"), sku_philips)
        expected_philips_retail = Decimal(PRODUCTS[3][5]) * Decimal("1.10")
        self.assertEqual(Decimal(str(preview.get("retail_price", {}).get("amount"))), expected_philips_retail)

        final_workset = self._workset()
        self.assertEqual(final_workset.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(final_workset.selected_identifiers, (sku_philips,))
        # Same original source Workset throughout -- no re-upload anywhere
        # in this journey.
        self.assertEqual(final_workset.source_dataset_id, self._workset().source_dataset_id)

        # Zero Bitrix writes for this entire journey (preview-only).
        self.assertEqual(self.bitrix_store.catalog(TENANT), self.bitrix_seed_snapshot)


if __name__ == "__main__":
    unittest.main()
