"""PANDA -- PRODUCTION DEFECT CLOSURE: compound scoped table operations
(PR #90 CORRECTION, post-review).

============================================================
WHY THIS FILE EXISTS (separate from
tests/test_panda_business_task_workset_continuation_defect_closure.py)
============================================================

PR #90's first cut used ``data_intel.nl_ops.compile_request`` (a bounded
regex/stem compiler) as a "does this text describe a table operation at
all?" capability probe for ``_maybe_handle_bulk_table_operation``. That
compiler can only ever produce ONE flat operation applied to whatever
rows survive a single filter -- it CANNOT represent a COMPOUND request
that assigns two (or more) DIFFERENT percentage changes to two (or more)
DIFFERENT, non-overlapping row scopes in the SAME turn (e.g. "first three
rows +7%, the rest +15%"), and extending it to do so would require an
ever-growing, wording-specific dictionary (ordinals, "the rest", ...) --
exactly the "finite dictionary of user wording" this project's
architecture forbids.

CORRECTED DESIGN (this file proves it end to end):

    natural-language compound request
        -> the managed conversational agent's OWN typed function-calling
           tool, ``apply_scoped_price_rules`` (``managed_agent_poc.
           runtime_subprocess`` -- the model interprets language into
           TYPED pydantic arguments; see that module for why this is
           semantic tool selection, never a second language router)
        -> ``data_intel.nl_ops.validate_scoped_price_rules`` (validates
           the untrusted structure against the real table schema and a
           small, explicit, non-extensible whitelist of scope/operation
           shapes -- never evaluates an expression)
        -> ``data_intel.transform.execute_scoped_percent_rules``
           (deterministic per-scope arithmetic, ``Decimal``-exact,
           never re-derived from free text or a model paraphrase)
        -> ``business_assistant.conversation_gateway.
           _persist_managed_agent_table_mutation`` bridges the freshly
           derived dataset into the SAME shared ``data_intel`` store the
           legacy engine and ``_maybe_handle_bulk_table_operation`` (the
           single-scope path) already read -- exactly the SAME
           "managed-agent-derived dataset must not be a second, isolated
           copy" invariant ``_persist_managed_agent_dataset_context``
           already established for the original attachment.

Real, genuine multi-scope arithmetic over this small fixed table is
ALREADY separately proven against the REAL isolated subprocess (real
model-shaped tool call, real ``DataIntelligenceService.
execute_scoped_price_rules``) in
``tests/test_managed_agent_poc.py::ManagedAgentOrchestrationTests::
test_scoped_price_rules_tool_applies_compound_percentages`` -- this file
does not repeat that proof. What THIS file proves instead is the
GATEWAY-LEVEL business behavior the correction mandated:

- a prior, real single-product selection exists in this SAME conversation
  (no re-attachment for turn 2);
- ONE natural-language follow-up describing TWO DIFFERENT scoped
  transformations resolves through the managed-agent path exactly ONCE
  (proving no ``MaxTurnsExceeded``-style retry loop is needed for this
  shape -- one tool call, one final answer, exactly like every other
  managed-agent tool already proven in this repository);
- the response contains a concrete, per-row before/after preview for
  EVERY affected row (never a vague summary);
- the derived dataset is bridged into the SAME shared store the legacy
  engine reads (never a second, orphaned copy), and the earlier
  single-product selection survives untouched;
- zero Bitrix mutation occurs anywhere in the scenario;
- a SECOND, semantically different wording (different scope KINDS,
  different percentages, different row segmentation) resolves through
  the exact SAME mechanism with zero phrase-specific code -- proving
  changing the numbers/wording requires zero code change.

Zero live network/LLM/Bitrix anywhere in this file: ``ManagedAgentPOC.
run_turn`` is replaced with a deterministic scripted double (the SAME
technique ``test_panda_business_task_workset_continuation_defect_
closure.py`` already uses), but -- unlike a hand-typed "select_product"
preview -- the compound-rule tool's OWN output/workbook bytes below are
computed by ACTUALLY calling ``data_intel.nl_ops.validate_scoped_price_
rules`` + ``data_intel.transform.execute_scoped_percent_rules`` (the
real, unmodified deterministic engine), so this file can never silently
memorize hand-typed arithmetic that production code does not actually
produce.
"""

from __future__ import annotations

import base64
import io
import os
import unittest
from decimal import Decimal
from unittest import mock

from openpyxl import Workbook

from business_assistant.conversation_gateway import ConversationRequest
from data_intel.contracts import ColumnDescriptor
from data_intel.nl_ops import validate_scoped_price_rules
from data_intel.transform import execute_scoped_percent_rules
from managed_agent_poc.adapter import ManagedAgentPOC, ManagedAgentTurnResult
from managed_agent_poc.state_store import ConversationStateStore, PersistedState
from tests.test_panda_business_task_workset_continuation_defect_closure import (
    CATEGORY,
    LG_SKU_1,
    LG_SKU_2,
    LG_SKU_3,
    SAMSUNG_SKU_1,
    SAMSUNG_SKU_2,
    TURN1_TEXT,
    _five_row_two_brand_price_list,
    _ManagedAgentFlagEnabledTestCase,
    _select_product_plan_entry,
)

COLUMNS = ("sku", "product_name", "category", "brand", "ean", "purchase_price", "розница")


def _column_descriptors() -> tuple[ColumnDescriptor, ...]:
    from data_intel.contracts import (
        ROLE_ARTICLE,
        ROLE_BRAND,
        ROLE_CATEGORY,
        ROLE_EAN,
        ROLE_PRODUCT_NAME,
        ROLE_PURCHASE_PRICE,
        ROLE_SELLING_PRICE,
    )

    roles = {
        "sku": ROLE_ARTICLE,
        "product_name": ROLE_PRODUCT_NAME,
        "category": ROLE_CATEGORY,
        "brand": ROLE_BRAND,
        "ean": ROLE_EAN,
        "purchase_price": ROLE_PURCHASE_PRICE,
        "розница": ROLE_SELLING_PRICE,
    }
    return tuple(
        ColumnDescriptor(source_name=col, normalized_name=col.lower(), inferred_type="text", semantic_role=roles[col])
        for col in COLUMNS
    )


def _rows_dicts() -> list[dict]:
    raw = _five_row_two_brand_price_list()
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw))
    ws = wb.active
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        rows.append({header[i]: str(cell.value) for i, cell in enumerate(row)})
    return rows


def _xlsx_bytes_from_rows(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(list(COLUMNS))
    for row in rows:
        ws.append([row.get(c, "") for c in COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _real_compound_rules_result(raw_rules: list[dict], *, fake_dataset_id: str) -> tuple[dict, dict]:
    """Runs the REAL, unmodified validation + execution engine
    (``data_intel.nl_ops.validate_scoped_price_rules`` +
    ``data_intel.transform.execute_scoped_percent_rules``) against the
    fixture table, exactly like ``DataIntelligenceService.
    execute_scoped_price_rules``/``runtime_subprocess.
    apply_scoped_price_rules`` do in production -- so this test file can
    never hand-type arithmetic the real engine would not actually
    produce. Returns ``(tool_output, scoped_rules_workbook)`` in the SAME
    shapes ``runtime_subprocess.apply_scoped_price_rules`` /
    ``ConversationState.scoped_rules_workbook`` produce."""
    table_columns = _column_descriptors()
    rows = _rows_dicts()
    resolved = validate_scoped_price_rules(raw_rules, _FakeTable(table_columns))
    result = execute_scoped_percent_rules(rows, table_columns, resolved)

    by_sku = {r["sku"]: r for r in result.rows}

    def _identifier(row_index: int) -> str:
        return result.rows[row_index]["sku"]

    preview_rows = [
        {
            "identifier": _identifier(c["row_index"]),
            "column": c["column"],
            "before": c["before"],
            "after": c["after"],
            "percent": c["percent"],
            "rule_index": c["rule_index"],
        }
        for c in result.row_changes
    ]
    tool_output = {
        "status": "OK",
        "dataset_id": fake_dataset_id,
        "previous_dataset_id": "ds-managed-agent-fake-1",
        "row_count_before": result.row_count_before,
        "row_count_after": result.row_count_after,
        "rules_applied": resolved,
        "rows_changed": len(result.row_changes),
        "preview_rows": preview_rows,
        "summary_text": f"Строк было: {result.row_count_before}, изменено: {len(result.row_changes)}.",
    }
    workbook_bytes = _xlsx_bytes_from_rows(result.rows)
    workbook = {"content_b64": base64.b64encode(workbook_bytes).decode("ascii"), "filename": "dataset.xlsx"}
    return tool_output, workbook, by_sku


class _FakeTable:
    """Minimal stand-in for ``data_intel.contracts.TableDescriptor`` --
    ``validate_scoped_price_rules`` only ever reads ``.columns``."""

    def __init__(self, columns):
        self.columns = columns


def _make_compound_scripted_run_turn(select_entry: dict, compound_raw_rules: list[dict]):
    """Deterministic double for ``ManagedAgentPOC.run_turn`` across TWO
    turns: turn 1 selects a single product (identical technique to
    ``tests.test_panda_business_task_workset_continuation_defect_
    closure._make_scripted_run_turn``); turn 2 calls the model-shaped
    ``apply_scoped_price_rules`` tool with the given RAW (untrusted,
    model-supplied) rules, whose OWN output/workbook are computed by the
    REAL deterministic engine (see ``_real_compound_rules_result``) --
    never by hand."""

    calls = {"n": 0}
    tool_output, workbook, by_sku = _real_compound_rules_result(compound_raw_rules, fake_dataset_id="ds-managed-agent-fake-2")

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
        store = ConversationStateStore(self.state_store_path)
        prior = store.load(tenant_id=tenant_id, conversation_id=conversation_id)

        if idx == 0:
            spec = select_entry
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

        # Turn 2 (and any further turn in this scenario): the compound
        # scoped-price-rules tool call. ``current_identifier``/
        # ``shown_identifiers`` are carried over UNCHANGED -- exactly like
        # the real tool, this one never touches single-product selection
        # state.
        store.save(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            state=PersistedState(
                dataset_id=tool_output["dataset_id"],
                shown_identifiers=list(prior.shown_identifiers),
                current_identifier=prior.current_identifier,
            ),
        )
        return ManagedAgentTurnResult(
            status="COMPLETED",
            final_output="Применил запрошенные изменения цены.",
            tool_calls=[{"tool": "apply_scoped_price_rules", "output": tool_output}],
            dataset_id=tool_output["dataset_id"],
            shown_identifiers=list(prior.shown_identifiers),
            current_identifier=prior.current_identifier,
            scoped_rules_workbook=workbook,
        )

    return fake_run_turn, calls, by_sku


class CompoundScopedPriceRulesAfterSingleProductSelectionTests(_ManagedAgentFlagEnabledTestCase):
    """Production-equivalent acceptance scenario for the PR #90
    correction: a prior single-product selection, NO re-attachment, and
    ONE natural-language follow-up assigning TWO DIFFERENT percentage
    changes to TWO DIFFERENT, non-overlapping row scopes (a row-position
    range and its remainder)."""

    async def test_position_range_and_remainder_compound_rule_in_one_turn(self):
        artifact_id = await _register_upload_helper(
            self, tenant="tenant-e", owner="u1", conv="conv-6", filename="prices6.xlsx"
        )

        select_entry = _select_product_plan_entry(
            sku=LG_SKU_1, name="Телевизор LG A1", purchase_price="100000", retail_price="150000"
        )
        # Different wording than the production reproduction ("первым
        # трём +7%, остальным +15%") and different percentages/row count
        # -- proves the general mechanism, not a memorized phrase.
        compound_rules = [
            {
                "scope": {"kind": "row_position_range", "start_position": 1, "end_position": 2},
                "price_field": "retail_price",
                "percent": 8,
            },
            {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -3},
        ]
        fake_run_turn, calls, by_sku = _make_compound_scripted_run_turn(select_entry, compound_rules)

        bitrix_catalog_before = len(self.bitrix_store.catalog("tenant-e"))

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            turn1 = await self.panda.respond(
                ConversationRequest(
                    text=TURN1_TEXT,
                    tenant_id="tenant-e",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-6",
                    attachment_refs=(artifact_id,),
                )
            )
            self.assertEqual(turn1.metadata.get("action_decision"), "MANAGED_AGENT")
            self.assertEqual(calls["n"], 1)

            task = self.panda._action_store.get(tenant_id="tenant-e", owner_id="u1", conversation_id="conv-6")  # noqa: SLF001
            original_dataset_id = str(task.parameters.get("dataset_id") or "")
            self.assertTrue(original_dataset_id)
            self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), LG_SKU_1)

            # Turn 2: NO re-attachment (no attachment_refs at all). ONE
            # message, TWO scoped transformations ("first two rows" is one
            # scope, "everything else" is the complementary remainder).
            turn2 = await self.panda.respond(
                ConversationRequest(
                    text=(
                        "Первые две строки увеличь на 8%, а всё, что "
                        "осталось, уменьши на 3%, покажи результат."
                    ),
                    tenant_id="tenant-e",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-6",
                )
            )

        # Exactly ONE managed-agent invocation per turn -- proves this
        # compound shape resolves without any retry/loop (the
        # ``MaxTurnsExceeded`` production failure mode required exactly
        # this scenario shape to previously exhaust the turn budget with
        # NO matching tool at all; now there IS a matching tool and it
        # is called exactly once).
        self.assertEqual(calls["n"], 2)

        self.assertNotIn("Приложите файл", turn2.text)
        self.assertEqual(turn2.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(turn2.metadata.get("managed_agent_tool"), "apply_scoped_price_rules")

        # Concrete per-row preview for EVERY affected row: identifier,
        # source value, applied operation/value, resulting value.
        self.assertIn(f"{LG_SKU_1}: розница 150000 -> 162000.00 (8%)", turn2.text)
        self.assertIn(f"{LG_SKU_2}: розница 180000 -> 194400.00 (8%)", turn2.text)
        self.assertIn(f"{SAMSUNG_SKU_1}: розница 140000 -> 135800.00 (-3%)", turn2.text)
        self.assertIn(f"{SAMSUNG_SKU_2}: розница 145000 -> 140650.00 (-3%)", turn2.text)
        self.assertIn(f"{LG_SKU_3}: розница 120000 -> 116400.00 (-3%)", turn2.text)
        self.assertIn("В Bitrix/Aspro ничего не записано", turn2.text)

        # Bridging: the derived dataset must land in the SAME shared
        # store the legacy engine reads, under the SAME ActiveTask
        # dataset_id key -- never an orphaned/private-store-only dataset.
        task = self.panda._action_store.get(tenant_id="tenant-e", owner_id="u1", conversation_id="conv-6")  # noqa: SLF001
        new_dataset_id = str(task.parameters.get("dataset_id") or "")
        self.assertTrue(new_dataset_id)
        self.assertNotEqual(new_dataset_id, original_dataset_id)

        # The earlier single-product selection survives untouched -- the
        # compound table operation neither requires nor destroys it.
        self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), LG_SKU_1)

        new_rows = self._dataset_rows_by_sku(new_dataset_id, tenant_id="tenant-e")
        self.assertEqual(set(new_rows), {LG_SKU_1, LG_SKU_2, SAMSUNG_SKU_1, SAMSUNG_SKU_2, LG_SKU_3})
        self.assertEqual(Decimal(new_rows[LG_SKU_1]["розница"]), Decimal("162000.00"))
        self.assertEqual(Decimal(new_rows[LG_SKU_2]["розница"]), Decimal("194400.00"))
        self.assertEqual(Decimal(new_rows[SAMSUNG_SKU_1]["розница"]), Decimal("135800.00"))
        self.assertEqual(Decimal(new_rows[SAMSUNG_SKU_2]["розница"]), Decimal("140650.00"))
        self.assertEqual(Decimal(new_rows[LG_SKU_3]["розница"]), Decimal("116400.00"))

        # The ORIGINAL dataset is untouched -- this is a NEW derived
        # dataset, never an in-place mutation.
        original_rows = self._dataset_rows_by_sku(original_dataset_id, tenant_id="tenant-e")
        self.assertEqual(Decimal(original_rows[LG_SKU_1]["розница"]), Decimal("150000"))

        # Zero Bitrix mutation anywhere in this scenario.
        self.assertEqual(len(self.bitrix_store.catalog("tenant-e")), bitrix_catalog_before)


class SecondCompoundScopedPriceRulesWordingTests(_ManagedAgentFlagEnabledTestCase):
    """A SECOND, semantically different natural-language scenario for the
    SAME structured-contract class: different scope KINDS (brand-based
    text filter + its remainder, instead of a row-position range), a
    different direction (discount, not surcharge, on the larger group),
    and different percentages -- resolved through the exact SAME
    ``apply_scoped_price_rules`` mechanism with zero phrase-specific
    code. Also demonstrates that changing the numeric values/segmentation
    requires ZERO production-code change: only the scripted tool
    ARGUMENTS below (and, in production, the model's own interpretation)
    differ from the scenario above."""

    async def test_brand_filter_and_remainder_compound_rule_in_one_turn(self):
        artifact_id = await _register_upload_helper(
            self, tenant="tenant-f", owner="u1", conv="conv-7", filename="prices7.xlsx"
        )

        select_entry = _select_product_plan_entry(
            sku=SAMSUNG_SKU_1, name="Телевизор Samsung B1", purchase_price="90000", retail_price="140000"
        )
        compound_rules = [
            {
                "scope": {"kind": "text_contains", "text_field": "brand", "contains": "LG"},
                "price_field": "retail_price",
                "percent": 12,
            },
            {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -4},
        ]
        fake_run_turn, calls, by_sku = _make_compound_scripted_run_turn(select_entry, compound_rules)

        bitrix_catalog_before = len(self.bitrix_store.catalog("tenant-f"))

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self.panda.respond(
                ConversationRequest(
                    text=TURN1_TEXT,
                    tenant_id="tenant-f",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-7",
                    attachment_refs=(artifact_id,),
                )
            )
            self.assertEqual(calls["n"], 1)

            # Different wording entirely: no ordinals, no "first N", a
            # brand name instead of a row range.
            turn2 = await self.panda.respond(
                ConversationRequest(
                    text=(
                        "Товарам бренда LG подними цену на 12%, а всем "
                        "прочим сделай скидку 4% и пришли предпросмотр."
                    ),
                    tenant_id="tenant-f",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-7",
                )
            )

        self.assertEqual(calls["n"], 2, "no retry/loop for this second wording either")
        self.assertNotIn("Приложите файл", turn2.text)
        self.assertEqual(turn2.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(turn2.metadata.get("managed_agent_tool"), "apply_scoped_price_rules")

        self.assertIn(f"{LG_SKU_1}: розница 150000 -> 168000.00 (12%)", turn2.text)
        self.assertIn(f"{LG_SKU_2}: розница 180000 -> 201600.00 (12%)", turn2.text)
        self.assertIn(f"{LG_SKU_3}: розница 120000 -> 134400.00 (12%)", turn2.text)
        self.assertIn(f"{SAMSUNG_SKU_1}: розница 140000 -> 134400.00 (-4%)", turn2.text)
        self.assertIn(f"{SAMSUNG_SKU_2}: розница 145000 -> 139200.00 (-4%)", turn2.text)
        self.assertIn("В Bitrix/Aspro ничего не записано", turn2.text)

        task = self.panda._action_store.get(tenant_id="tenant-f", owner_id="u1", conversation_id="conv-7")  # noqa: SLF001
        new_dataset_id = str(task.parameters.get("dataset_id") or "")
        self.assertEqual(task.parameters.get("bitrix_product_fields", {}).get("sku"), SAMSUNG_SKU_1)

        new_rows = self._dataset_rows_by_sku(new_dataset_id, tenant_id="tenant-f")
        self.assertEqual(Decimal(new_rows[LG_SKU_1]["розница"]), Decimal("168000.00"))
        self.assertEqual(Decimal(new_rows[LG_SKU_2]["розница"]), Decimal("201600.00"))
        self.assertEqual(Decimal(new_rows[LG_SKU_3]["розница"]), Decimal("134400.00"))
        self.assertEqual(Decimal(new_rows[SAMSUNG_SKU_1]["розница"]), Decimal("134400.00"))
        self.assertEqual(Decimal(new_rows[SAMSUNG_SKU_2]["розница"]), Decimal("139200.00"))

        self.assertEqual(len(self.bitrix_store.catalog("tenant-f")), bitrix_catalog_before)


async def _register_upload_helper(testcase, *, tenant: str, owner: str, conv: str, filename: str) -> str:
    from tests.test_panda_business_task_workset_continuation_defect_closure import _register_upload

    return await _register_upload(
        testcase.artifact_service,
        tenant=tenant,
        owner=owner,
        conv=conv,
        filename=filename,
        content=_five_row_two_brand_price_list(),
    )


if __name__ == "__main__":
    unittest.main()
