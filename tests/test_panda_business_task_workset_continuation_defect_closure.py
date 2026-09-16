"""PANDA -- PRODUCTION DEFECT CLOSURE: business task ownership / workset
continuation (post PR #89).

============================================================
PRODUCTION FAILURE (proven root cause, static read-only audit)
============================================================

Real production conversation (``PANDA_MANAGED_AGENT_ENABLED=true``):

    Turn 1: XLSX attached, "prepare one product from this price list" ->
        the Managed Agent path (``managed_agent_poc``) selects ONE
        product and previews it. ``action_decision == "MANAGED_AGENT"``.
    Turn 2 (no re-attachment): "show me the whole file first" -> Panda
        recognizes N products.
    Turn 3 (no re-attachment): a bulk, multi-row calculation over the
        ALREADY-attached spreadsheet (e.g. "increase retail price for
        [some subset] by X%, ... for the rest by Y%, show me a preview") ->
        production replied "Приложите файл Excel/CSV, чтобы я мог его
        обработать." (please attach the file) even though one was already
        attached, and logged:
            managed_agent_poc: eligible turn (...) fell back to the legacy
            conversational path -- run_turn returned status=ERROR
            error=MaxTurnsExceeded: Max turns (10) exceeded
    Turn 4: re-attaching the SAME file produced only a generic whole-sheet
        summary (rows/columns/min/max/avg), never the requested
        transformation.

ROOT CAUSE (proven by static code audit, see the PR description / final
report for the full FILE:LINE trace):

1. ``managed_agent_poc`` ingests an attached spreadsheet into its OWN
   private, per-conversation SQLite dataset store
   (``managed_agent_poc.panda_bridge._durable_paths`` ->
   ``PANDA_DATA_DIR/managed_agent/<tenant>/<conversation>/dataset.
   sqlite3``) -- a store that is PHYSICALLY SEPARATE from whatever store
   the legacy ``resolve_action_turn``/FAMILY_EXCEL/``data.
   excel_assistant`` path's shared ``DataIntelligenceService`` is wired
   to. A dataset_id minted by the managed-agent path is meaningless to
   the legacy engine.
2. ``business_assistant.conversation_gateway._persist_managed_agent_
   product_context`` (PR #87/#88) only ever persisted product-selection
   fields onto ``ActiveTaskStore`` -- NEVER a ``dataset_id`` -- so a
   later turn that falls back out of the managed agent (disabled,
   ``MaxTurnsExceeded``, ...) finds an active FAMILY_EXCEL task with NO
   usable ``dataset_id`` and is told to re-attach a file it already has
   (``resolve_action_turn``'s own, pre-existing, ``ASK_CLARIFICATION``
   "Приложите файл..." branch, unchanged -- see
   ``business_assistant/action_continuation.py``).
3. The managed agent's tool set (``analyze_spreadsheet``/
   ``select_product``/``explain_bitrix_write_plan`` --
   ``managed_agent_poc/runtime_subprocess.py``) has NO tool for a
   bulk/multi-row calculation. Routed a bulk request anyway, it has no
   matching tool to call and, in production, spent enough internal turns
   trying before giving up that it exceeded the Agents SDK's turn budget
   (``MaxTurnsExceeded``).

MINIMAL FIX (``business_assistant/conversation_gateway.py``, see that
file's own new-method docstrings for the full rationale):

A) ``_persist_managed_agent_dataset_context``: whenever a managed-agent
   turn used a FRESH spreadsheet attachment, ingest that SAME artifact
   into the SAME shared ``data_intel`` store the legacy engine already
   reads -- by dispatching the EXISTING ``data.excel_assistant``/
   ``assist`` tool through ``self._tool_gateway`` (the SAME tool/
   ingestion code ``_invoke_tool`` already uses -- no second parser, no
   new store) -- and persist the resulting SHARED ``dataset_id`` onto
   ``ActiveTaskStore`` under the SAME existing
   ``parameters["dataset_id"]`` key the legacy ``ROW_FOUND`` handler
   already uses.
B) ``_maybe_handle_bulk_table_operation``: BEFORE the managed agent is
   ever invoked for a turn, ask -- using the SAME existing deterministic
   ``data_intel.nl_ops`` compiler the legacy engine already uses for this
   exact purpose -- whether this turn's free text compiles into a real,
   executable table-wide operation against the conversation's already-
   ingested dataset. If so, the SAME existing tool call that would
   eventually run it anyway is dispatched directly and its
   ALREADY-COMPUTED real answer is returned immediately, so the managed
   agent is never entered for this turn at all (no wasted turns, no
   ``MaxTurnsExceeded``). This is a CAPABILITY probe (does the existing
   deterministic compiler recognize a structured operation in this
   text?), never a phrase/keyword allow-list -- no percentage, row
   count, product identity, or wording is hardcoded anywhere in this
   fix.

Both fixes reuse 100% EXISTING deterministic code
(``data_intel.nl_ops.compile_request`` / ``data_intel.transform.
execute_plan`` / ``data.excel_assistant`` tool / ``ActiveTaskStore`` /
``format_tool_user_text`` / ``mark_executed``) -- no new state store, no
new pricing/spreadsheet math, no new agent/router.

============================================================
WHAT THIS FILE PROVES (general behavior, not memorized reproduction)
============================================================

- ``BulkTableOperationAfterSingleProductSelectionTests``: the production-
  equivalent acceptance scenario -- attach a multi-row, multi-brand
  spreadsheet, preview ONE product via the managed agent, then (same
  conversation, NO re-attachment) ask for a bulk calculation filtered by
  brand. Proves: no re-attachment requested, the managed agent is never
  even invoked for that turn (so no ``MaxTurnsExceeded`` risk), the
  correct SUBSET of rows is transformed with the correct arithmetic, the
  previously-selected single product's own state is untouched, and zero
  Bitrix mutation occurs.
- ``SecondBulkOperationSegmentationScenarioTests``: a SECOND, semantically
  different natural-language scenario -- different wording, different
  percent, different row-segmentation criterion (price threshold instead
  of brand, decrease instead of increase) -- proving the SAME mechanism
  generalizes without any phrase-specific code.
- ``ManagedAgentFailureFallbackPreservesDatasetContextTests``: when the
  managed agent DOES get invoked (a plain, non-bulk follow-up its probe
  correctly does not intercept) and fails for any reason (simulating the
  production ``MaxTurnsExceeded`` -> ``status=ERROR`` shape), the legacy
  fallback resolves the SAME already-attached dataset -- it never asks
  the user to re-attach the file.
- ``RegressionExistingBehaviorPreservedTests``: existing single-product
  preparation and the PR #87/#88 governed Bitrix write confirmation path
  are completely unaffected by these two additive checks.

Zero live network/LLM/Bitrix anywhere in this file: ``ManagedAgentPOC.
run_turn`` is replaced with a deterministic scripted double (the same
technique already proven in
``tests/test_panda_managed_agent_enrichment_delegation.py``); the Bitrix
bridge is backed by the in-memory fixture adapter.
"""

from __future__ import annotations

import io
import os
import unittest
from decimal import Decimal
from unittest import mock

from openpyxl import Workbook

from business_assistant.conversation_gateway import ConversationRequest
from managed_agent_poc.adapter import ManagedAgentPOC, ManagedAgentTurnResult
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from managed_agent_poc.state_store import ConversationStateStore, PersistedState
from tests.test_panda_product_enrichment_conversational import _bitrix_bridge, _panda, _register_upload

CATEGORY = "TV"

LG_SKU_1 = "LG-A1"
LG_SKU_2 = "LG-A2"
LG_SKU_3 = "LG-A3"
SAMSUNG_SKU_1 = "SS-B1"
SAMSUNG_SKU_2 = "SS-B2"

TURN1_TEXT = (
    "Подготовь один телевизор из этого прайса. Ничего пока не записывай и не публикуй в Bitrix."
)


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _five_row_two_brand_price_list() -> bytes:
    return _xlsx_bytes(
        [
            [LG_SKU_1, "Телевизор LG A1", CATEGORY, "LG", "1000000000001", "100000", "150000"],
            [LG_SKU_2, "Телевизор LG A2", CATEGORY, "LG", "1000000000002", "120000", "180000"],
            [SAMSUNG_SKU_1, "Телевизор Samsung B1", CATEGORY, "Samsung", "1000000000003", "90000", "140000"],
            [SAMSUNG_SKU_2, "Телевизор Samsung B2", CATEGORY, "Samsung", "1000000000004", "95000", "145000"],
            [LG_SKU_3, "Телевизор LG A3", CATEGORY, "LG", "1000000000005", "80000", "120000"],
        ]
    )


def _make_scripted_run_turn(plan: list[dict]):
    """Deterministic double for ``ManagedAgentPOC.run_turn`` (same
    technique as ``tests.test_panda_managed_agent_enrichment_delegation.
    _make_fake_run_turn``): never launches the isolated SDK subprocess,
    never touches the network. Each call consumes the next ``plan`` entry
    in order; a plan entry with ``"error": True`` reproduces the exact
    production ``status=ERROR`` shape a real ``MaxTurnsExceeded`` (or any
    other real-subprocess failure) surfaces as, WITHOUT this test needing
    to actually exhaust the real Agents SDK's turn budget."""

    calls = {"n": 0}

    def fake_run_turn(
        self,
        *,
        text,
        tenant_id,
        owner_id="",
        conversation_id,
        dataset_id="",
        artifact_bytes_path="",
        artifact_filename="",
        test_scripted_plan=None,
        timeout_s=60.0,
    ):
        idx = calls["n"]
        calls["n"] += 1
        spec = plan[idx]

        if spec.get("error"):
            return ManagedAgentTurnResult(status="ERROR", error=spec.get("error_text", "MaxTurnsExceeded: Max turns (10) exceeded"))

        store = ConversationStateStore(self.state_store_path)
        prior = store.load(tenant_id=tenant_id, conversation_id=conversation_id)
        new_dataset_id = prior.dataset_id or "ds-managed-agent-fake-1"
        current_identifier = spec.get("current_identifier") or prior.current_identifier
        shown = list(prior.shown_identifiers)
        if current_identifier and current_identifier not in shown:
            shown.append(current_identifier)
        store.save(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            state=PersistedState(dataset_id=new_dataset_id, shown_identifiers=shown, current_identifier=current_identifier),
        )
        return ManagedAgentTurnResult(
            status="COMPLETED",
            final_output=spec.get("final_output", ""),
            tool_calls=spec["tool_calls"],
            dataset_id=new_dataset_id,
            shown_identifiers=shown,
            current_identifier=current_identifier,
        )

    return fake_run_turn, calls


def _select_product_plan_entry(*, sku: str, name: str, purchase_price: str, retail_price: str) -> dict:
    return {
        "current_identifier": sku,
        "tool_calls": [
            {
                "tool": "select_product",
                "output": {
                    "status": "SELECTED",
                    "matched_by": "next_unspecified",
                    "name": name,
                    "sku": sku,
                    "ean": "1000000000001",
                    "category": CATEGORY,
                    "brand": "LG" if sku.startswith("LG") else "Samsung",
                    "purchase_price": purchase_price,
                    "retail_price": retail_price,
                },
            }
        ],
        "final_output": f"Товар {name}, розничная цена {retail_price}.",
    }


class _ManagedAgentFlagEnabledTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        self.bridge, self.bitrix_store = _bitrix_bridge()
        self.panda, self.artifact_service = _panda(bitrix_bridge=self.bridge)

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
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _data_intel_service(self):
        adapters = {row.descriptor.tool_id: row.adapter for row in self.panda._tool_gateway.registry._items.values()}  # noqa: SLF001
        return adapters["data.excel_assistant"]._svc  # noqa: SLF001

    def _dataset_rows_by_sku(self, dataset_id: str, *, tenant_id: str) -> dict:
        """Row lookup keyed by ``sku`` for assertions -- resolves the real
        ``table_id`` from the dataset descriptor rather than assuming a
        fixed value (the actual ingested/derived table_id is a composite
        string like ``"Sheet:t0"``, not a bare integer)."""

        svc = self._data_intel_service()
        desc = svc.store.get_dataset(dataset_id, tenant_id=tenant_id)
        table_id = desc.tables[0].table_id
        rows = svc.store.get_rows(dataset_id, tenant_id=tenant_id, table_id=table_id)
        return {r["sku"]: r for r in rows}


class BulkTableOperationAfterSingleProductSelectionTests(_ManagedAgentFlagEnabledTestCase):
    """Production-equivalent acceptance scenario."""

    async def test_bulk_brand_filtered_price_increase_uses_already_attached_sheet(self):
        artifact_id = await _register_upload(
            self.artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="conv-1",
            filename="prices.xlsx",
            content=_five_row_two_brand_price_list(),
        )

        plan = [
            _select_product_plan_entry(
                sku=LG_SKU_1, name="Телевизор LG A1", purchase_price="100000", retail_price="150000"
            ),
            {"error": True},  # must NEVER be consumed -- see assertion below
        ]
        fake_run_turn, calls = _make_scripted_run_turn(plan)

        bitrix_catalog_before = len(self.bitrix_store.catalog("tenant-a"))

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            turn1 = await self.panda.respond(
                ConversationRequest(
                    text=TURN1_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-1",
                    attachment_refs=(artifact_id,),
                )
            )
            self.assertEqual(turn1.metadata.get("action_decision"), "MANAGED_AGENT")
            self.assertEqual(calls["n"], 1)

            # Fix A: the managed-agent turn must have ALSO established a
            # dataset_id the legacy engine can resolve -- never asking the
            # user to re-attach a file it already has.
            task = self.panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="conv-1")  # noqa: SLF001
            self.assertIsNotNone(task)
            original_dataset_id = str(task.parameters.get("dataset_id") or "")
            self.assertTrue(original_dataset_id, "dataset_id must be persisted onto ActiveTaskStore")
            self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), LG_SKU_1)

            # Different wording than the production reproduction, different
            # percent, different brand -- proves general mechanism, not a
            # memorized phrase.
            turn2 = await self.panda.respond(
                ConversationRequest(
                    text=(
                        "Оставь только LG, подними розничную цену на 10 "
                        "процентов и пришли предпросмотр."
                    ),
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-1",
                )
            )

        # The managed agent must NEVER have been invoked a second time --
        # this is the "no MaxTurnsExceeded" assertion: if this turn had
        # been routed into the managed agent, the SECOND (``error``) plan
        # entry would have been consumed.
        self.assertEqual(calls["n"], 1, "the bulk operation must be resolved WITHOUT ever invoking the managed agent")

        self.assertNotIn("Приложите файл", turn2.text)
        self.assertEqual(turn2.metadata.get("action_decision"), "CALL_TOOL")
        self.assertIn("Строк было: 5", turn2.text)
        self.assertIn("стало: 3", turn2.text)

        task = self.panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="conv-1")  # noqa: SLF001
        new_dataset_id = str(task.parameters.get("dataset_id") or "")
        self.assertTrue(new_dataset_id)
        self.assertNotEqual(new_dataset_id, original_dataset_id)
        # Single-product state from turn 1 must remain intact (untouched by
        # the bulk table operation) -- proves the bulk operation neither
        # requires nor destroys the earlier single-product selection.
        self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), LG_SKU_1)

        new_rows = self._dataset_rows_by_sku(new_dataset_id, tenant_id="tenant-a")
        self.assertEqual(set(new_rows), {LG_SKU_1, LG_SKU_2, LG_SKU_3}, "only LG rows must survive the filter")
        self.assertEqual(Decimal(new_rows[LG_SKU_1]["розница"]), Decimal("165000.00"))
        self.assertEqual(Decimal(new_rows[LG_SKU_2]["розница"]), Decimal("198000.00"))
        self.assertEqual(Decimal(new_rows[LG_SKU_3]["розница"]), Decimal("132000.00"))

        # The ORIGINAL dataset (and its Samsung rows) must be untouched --
        # this is a NEW derived dataset, never an in-place mutation.
        original_rows = self._dataset_rows_by_sku(original_dataset_id, tenant_id="tenant-a")
        self.assertEqual(len(original_rows), 5)
        self.assertEqual(Decimal(original_rows[LG_SKU_1]["розница"]), Decimal("150000"))
        self.assertEqual(Decimal(original_rows[SAMSUNG_SKU_1]["розница"]), Decimal("140000"))

        # Zero Bitrix mutation anywhere in this scenario -- ``catalog()``
        # lazily seeds a fixed set of unrelated demo products for any
        # never-before-seen tenant (``BitrixCatalogStore._seed_catalog``),
        # so a fresh-tenant baseline (captured before either turn) is the
        # correct zero-mutation check, not an absolute empty catalog.
        self.assertEqual(len(self.bitrix_store.catalog("tenant-a")), bitrix_catalog_before)


class SecondBulkOperationSegmentationScenarioTests(_ManagedAgentFlagEnabledTestCase):
    """A SECOND, semantically different natural-language scenario: a
    different filter criterion (price threshold, not brand), a decrease
    instead of an increase, and different values -- must pass through the
    SAME mechanism (``_maybe_handle_bulk_table_operation``) with no
    phrase-specific code added."""

    async def test_bulk_price_threshold_discount_uses_already_attached_sheet(self):
        artifact_id = await _register_upload(
            self.artifact_service,
            tenant="tenant-b",
            owner="u1",
            conv="conv-2",
            filename="prices2.xlsx",
            content=_five_row_two_brand_price_list(),
        )
        plan = [
            _select_product_plan_entry(
                sku=SAMSUNG_SKU_1, name="Телевизор Samsung B1", purchase_price="90000", retail_price="140000"
            )
        ]
        fake_run_turn, calls = _make_scripted_run_turn(plan)
        bitrix_catalog_before = len(self.bitrix_store.catalog("tenant-b"))

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self.panda.respond(
                ConversationRequest(
                    text=TURN1_TEXT,
                    tenant_id="tenant-b",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-2",
                    attachment_refs=(artifact_id,),
                )
            )
            self.assertEqual(calls["n"], 1)

            turn2 = await self.panda.respond(
                ConversationRequest(
                    text="Для товаров дороже 145000 снизь розничную цену на 5%.",
                    tenant_id="tenant-b",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-2",
                )
            )

        self.assertEqual(calls["n"], 1, "no second managed-agent invocation for the bulk turn")
        self.assertNotIn("Приложите файл", turn2.text)
        self.assertEqual(turn2.metadata.get("action_decision"), "CALL_TOOL")
        # Only rows with retail price strictly above 145000 survive the
        # filter: LG_SKU_1 (150000) and LG_SKU_2 (180000). SS_SKU_2 (exactly
        # 145000) is correctly excluded by the strict ">" comparison.
        self.assertIn("стало: 2", turn2.text)

        task = self.panda._action_store.get(tenant_id="tenant-b", owner_id="u1", conversation_id="conv-2")  # noqa: SLF001
        new_dataset_id = str(task.parameters.get("dataset_id") or "")
        new_rows = self._dataset_rows_by_sku(new_dataset_id, tenant_id="tenant-b")
        self.assertEqual(set(new_rows), {LG_SKU_1, LG_SKU_2})
        self.assertEqual(Decimal(new_rows[LG_SKU_1]["розница"]), Decimal("142500.00"))
        self.assertEqual(Decimal(new_rows[LG_SKU_2]["розница"]), Decimal("171000.00"))
        self.assertEqual(len(self.bitrix_store.catalog("tenant-b")), bitrix_catalog_before)


class ManagedAgentFailureFallbackPreservesDatasetContextTests(_ManagedAgentFlagEnabledTestCase):
    """When the managed agent's own probe correctly does NOT intercept a
    turn (a plain, non-bulk follow-up) and the managed agent itself then
    fails (the exact ``status=ERROR`` shape a real ``MaxTurnsExceeded``
    surfaces as), the legacy fallback must resolve the SAME already-
    attached dataset -- never asking the user to re-attach it."""

    async def test_managed_agent_error_falls_back_without_losing_dataset_context(self):
        artifact_id = await _register_upload(
            self.artifact_service,
            tenant="tenant-c",
            owner="u1",
            conv="conv-3",
            filename="prices3.xlsx",
            content=_five_row_two_brand_price_list(),
        )
        plan = [
            _select_product_plan_entry(
                sku=LG_SKU_1, name="Телевизор LG A1", purchase_price="100000", retail_price="150000"
            ),
            {"error": True},
        ]
        fake_run_turn, calls = _make_scripted_run_turn(plan)

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self.panda.respond(
                ConversationRequest(
                    text=TURN1_TEXT,
                    tenant_id="tenant-c",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-3",
                    attachment_refs=(artifact_id,),
                )
            )

            # A plain question with no filter/percent/sort/etc. keyword --
            # the bulk-operation probe correctly returns None (nl_ops
            # cannot compile it), so the managed agent IS invoked, and IS
            # made to fail here to reproduce the production MaxTurnsExceeded
            # shape without needing the real Agents SDK turn budget.
            turn2 = await self.panda.respond(
                ConversationRequest(
                    text="Дай сводку по данным из файла: количество строк и диапазон цен.",
                    tenant_id="tenant-c",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-3",
                )
            )

        self.assertEqual(calls["n"], 2, "the managed agent must have been attempted (and failed) for this turn")
        self.assertNotIn("Приложите файл", turn2.text, "the already-attached spreadsheet must not be forgotten")
        self.assertEqual(turn2.metadata.get("action_decision"), "CALL_TOOL")
        self.assertIn("5 строк", turn2.text)


class RegressionExistingBehaviorPreservedTests(_ManagedAgentFlagEnabledTestCase):
    """Existing single-product managed-agent preparation, and the PR
    #87/#88 governed Bitrix write path, are unaffected by the two new
    additive checks."""

    async def test_single_product_preparation_unaffected(self):
        artifact_id = await _register_upload(
            self.artifact_service,
            tenant="tenant-d",
            owner="u1",
            conv="conv-4",
            filename="prices4.xlsx",
            content=_five_row_two_brand_price_list(),
        )
        plan = [
            _select_product_plan_entry(
                sku=LG_SKU_2, name="Телевизор LG A2", purchase_price="120000", retail_price="180000"
            )
        ]
        fake_run_turn, calls = _make_scripted_run_turn(plan)
        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            result = await self.panda.respond(
                ConversationRequest(
                    text=TURN1_TEXT,
                    tenant_id="tenant-d",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-4",
                    attachment_refs=(artifact_id,),
                )
            )
        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(calls["n"], 1)
        self.assertIn(LG_SKU_2, result.text)

    async def test_explicit_write_confirmation_still_reaches_governed_bitrix_write(self):
        from business_assistant.action_continuation import CALL_CONTROLLED_BITRIX_WRITE

        # ``_bitrix_bridge()`` (shared fixture, ``tests.test_panda_product_
        # enrichment_conversational``) only activates a Bitrix connection for
        # "tenant-a" -- the SAME tenant every other governed-write regression
        # test in this repository uses with this fixture.
        artifact_id = await _register_upload(
            self.artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="conv-5",
            filename="prices5.xlsx",
            content=_five_row_two_brand_price_list(),
        )
        plan = [
            _select_product_plan_entry(
                sku=LG_SKU_1, name="Телевизор LG A1", purchase_price="100000", retail_price="150000"
            )
        ]
        fake_run_turn, calls = _make_scripted_run_turn(plan)
        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self.panda.respond(
                ConversationRequest(
                    text=TURN1_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-5",
                    attachment_refs=(artifact_id,),
                )
            )
            before = len(self.bitrix_store.catalog("tenant-a"))
            # An explicit write confirmation must skip the managed agent
            # entirely (PR #87 PART 3, unchanged) and reach the governed
            # write path -- never intercepted by the new bulk-op probe
            # either (it is gated behind the SAME
            # ``managed_agent_enabled() and not is_explicit_bitrix_write_
            # confirmation(text)`` condition).
            result = await self.panda.respond(
                ConversationRequest(
                    text="Подтверждаю: создай этот товар в Bitrix по показанному плану.",
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-5",
                )
            )

        self.assertEqual(calls["n"], 1, "an explicit write confirmation must never invoke the managed agent")
        self.assertEqual(result.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        self.assertEqual(len(self.bitrix_store.catalog("tenant-a")) - before, 1)


if __name__ == "__main__":
    unittest.main()
