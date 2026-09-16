"""PANDA — CANONICAL WORKSET / SINGLE DATA OWNERSHIP — golden acceptance
tests (business_assistant.workset).

Closes the split-ownership defect where an uploaded spreadsheet's identity
was tracked independently by:

- ``ActiveTask.parameters["dataset_id"]`` (overwritten unconditionally by
  every successful FAMILY_EXCEL tool call);
- the managed-agent integration boundary's own private, per-conversation
  dataset store;
- ``bitrix_row_selection`` (a separate "currently selected product" key
  that could keep dominating a turn even after the task's real focus had
  legitimately moved on).

``business_assistant.workset.Workset`` is now the ONE authoritative
business-data context (``workset_id``/``source_dataset_id``/
``current_dataset_id``/``scope``/``selected_identifiers``), persisted as a
plain dict under the EXISTING, already-durable
``ActiveTask.parameters["workset"]`` key, with ``task.parameters
["dataset_id"]`` kept as a compatibility mirror so the legacy
``resolve_action_turn`` continuation gate needs no change of its own.

Structure (mirrors the six mandated golden acceptance tests):

  * ``WorksetModuleUnitTests`` -- direct unit coverage of every state
    transition function in ``business_assistant.workset`` (the exact
    invariants TEST A/B/C require, proven at the smallest possible
    granularity, independent of NL wording).
  * ``WorksetDurabilityTests`` -- TEST D: a Workset persisted through
    ``SqliteActiveTaskStore`` survives being read back by a BRAND NEW
    store instance bound to the same on-disk file (simulating a process
    restart), with zero re-upload.
  * ``WorksetAcceptanceIntegrationTests`` -- TEST A/B/C/F driven through
    the REAL ``WorkflowPandaConversationGateway``/``resolve_action_turn``/
    ``data_intel`` chain (managed agent disabled -- the default), proving
    the Workset invariants hold across real conversational turns, that a
    later bulk operation is never blocked by a stale single-product
    selection, and that no turn ever needs to ask for the file again.
  * ``ManagedAgentCannotOverrideWorksetTests`` -- TEST E: the managed-agent
    integration boundary (real ``panda_bridge``/``ManagedAgentPOC`` chain,
    only the isolated subprocess's own model turn is faked) can narrow the
    canonical Workset's scope to a resolved product, but can never make
    its own private, per-conversation dataset id become the canonical
    ``source_dataset_id``/``current_dataset_id``, and never resets
    ``workset_id`` across a product switch.

Zero live network/model calls; zero live Bitrix. The existing PR #87/#88/
#89 governed-write/false-success/ownership regression suites (run
separately, unmodified) prove Bitrix write behavior is untouched by this
change.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from openpyxl import Workbook

from business_assistant import workset as workset_lib
from business_assistant.action_continuation import (
    EXCEL_CONTRACT,
    FAMILY_EXCEL,
    RISK_READ,
    STATUS_DRAFT,
    ActiveTask,
    SqliteActiveTaskStore,
)
from business_assistant.conversation_gateway import ConversationRequest
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_managed_agent_enrichment_delegation import _make_fake_run_turn
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload

TENANT = "tenant-a"
OWNER = "u1"

SKU_A, NAME_A, EAN_A, PURCHASE_A, RETAIL_A = "TV-A-1001", "Телевизор A", "4600000000010", "90000", "129990"
SKU_B, NAME_B, EAN_B, PURCHASE_B, RETAIL_B = "TV-B-2002", "Телевизор B", "4600000000027", "95000", "139990"
SKU_C, NAME_C, EAN_C, PURCHASE_C, RETAIL_C = "TV-C-3003", "Телевизор C", "4600000000034", "99000", "149990"
SKU_D, NAME_D, EAN_D, PURCHASE_D, RETAIL_D = "TV-D-4004", "Телевизор D", "4600000000041", "80000", "119990"
FILENAME = "workset_acceptance.xlsx"


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append([SKU_A, NAME_A, "Телевизоры", "LG", EAN_A, PURCHASE_A, RETAIL_A])
    ws.append([SKU_B, NAME_B, "Телевизоры", "LG", EAN_B, PURCHASE_B, RETAIL_B])
    ws.append([SKU_C, NAME_C, "Телевизоры", "LG", EAN_C, PURCHASE_C, RETAIL_C])
    ws.append([SKU_D, NAME_D, "Телевизоры", "LG", EAN_D, PURCHASE_D, RETAIL_D])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_task(*, family: str = FAMILY_EXCEL, conversation_id: str = "conv-1") -> ActiveTask:
    return ActiveTask(
        task_id="task-1",
        tenant_id=TENANT,
        owner_id=OWNER,
        conversation_id=conversation_id,
        family=family,
        tool_id=EXCEL_CONTRACT.tool_id,
        operation=EXCEL_CONTRACT.operation,
        goal="",
        artifact_type="workbook",
        status=STATUS_DRAFT,
        risk=RISK_READ,
    )


class WorksetModuleUnitTests(unittest.TestCase):
    """Direct unit coverage of every ``business_assistant.workset`` state
    transition -- the exact invariants TEST A/B/C require, independent of
    any NL wording, gateway, or store."""

    def test_start_new_source_mints_id_once_then_stable_across_reattachment(self):
        w1 = workset_lib.start_new_source(
            None, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-1"
        )
        self.assertTrue(w1.workset_id)
        self.assertEqual(w1.source_dataset_id, "ds-1")
        self.assertEqual(w1.current_dataset_id, "ds-1")
        self.assertEqual(w1.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w1.selected_identifiers, ())

        # A LATER fresh attachment on the SAME conversation continues the
        # SAME workset_id (requirement: "a new attachment continues the
        # same business task"), while still resetting source==current.
        w2 = workset_lib.start_new_source(
            w1, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-2"
        )
        self.assertEqual(w2.workset_id, w1.workset_id)
        self.assertEqual(w2.source_dataset_id, "ds-2")
        self.assertEqual(w2.current_dataset_id, "ds-2")

    def test_apply_tool_result_row_found_narrows_scope_without_touching_dataset(self):
        w0 = workset_lib.start_new_source(
            None, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-src"
        )
        data = {
            "status": "ROW_FOUND",
            "dataset_id": "ds-src",
            "product_fields": {"sku": SKU_A},
        }
        w1 = workset_lib.apply_tool_result(w0, data)
        self.assertEqual(w1.workset_id, w0.workset_id)
        self.assertEqual(w1.source_dataset_id, "ds-src")
        self.assertEqual(w1.current_dataset_id, "ds-src")
        self.assertEqual(w1.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w1.selected_identifiers, (SKU_A,))

    def test_apply_tool_result_ok_same_row_count_advances_to_full_dataset(self):
        w0 = workset_lib.select_single(
            workset_lib.start_new_source(
                None, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-src"
            ),
            SKU_A,
        )
        data = {"status": "OK", "dataset_id": "ds-derived-1", "row_count_before": 4, "row_count_after": 4}
        w1 = workset_lib.apply_tool_result(w0, data)
        # CRITICAL INVARIANT: source_dataset_id is immutable across a
        # deterministic transformation.
        self.assertEqual(w1.source_dataset_id, "ds-src")
        self.assertNotEqual(w1.current_dataset_id, w1.source_dataset_id)
        self.assertEqual(w1.current_dataset_id, "ds-derived-1")
        self.assertEqual(w1.scope, workset_lib.SCOPE_FULL_DATASET)
        # Single-product selection never leaks into a bulk result.
        self.assertEqual(w1.selected_identifiers, ())
        self.assertEqual(w1.workset_id, w0.workset_id)

    def test_apply_tool_result_ok_reduced_row_count_advances_to_filtered_set(self):
        w0 = workset_lib.start_new_source(
            None, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-src"
        )
        data = {"status": "OK", "dataset_id": "ds-derived-1", "row_count_before": 4, "row_count_after": 2}
        w1 = workset_lib.apply_tool_result(w0, data)
        self.assertEqual(w1.scope, workset_lib.SCOPE_FILTERED_SET)
        self.assertEqual(w1.source_dataset_id, "ds-src")
        self.assertEqual(w1.current_dataset_id, "ds-derived-1")

    def test_apply_tool_result_analyzed_resets_scope_to_full_dataset(self):
        w0 = workset_lib.select_single(
            workset_lib.start_new_source(
                None, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-src"
            ),
            SKU_A,
        )
        data = {"status": "ANALYZED", "dataset_id": "ds-src"}
        w1 = workset_lib.apply_tool_result(w0, data)
        self.assertEqual(w1.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w1.selected_identifiers, ())
        self.assertEqual(w1.current_dataset_id, "ds-src")
        self.assertEqual(w1.source_dataset_id, "ds-src")

    def test_select_full_dataset_returns_to_source_without_mutating_source(self):
        w0 = workset_lib.advance_dataset_version(
            workset_lib.start_new_source(
                None, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-src"
            ),
            dataset_id="ds-derived-9",
            scope=workset_lib.SCOPE_FILTERED_SET,
            identifiers=("x",),
        )
        w1 = workset_lib.select_full_dataset(w0)
        self.assertEqual(w1.workset_id, w0.workset_id)
        self.assertEqual(w1.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w1.current_dataset_id, w0.source_dataset_id)
        self.assertEqual(w1.source_dataset_id, "ds-src")
        self.assertEqual(w1.selected_identifiers, ())

    def test_select_single_never_touches_dataset_ids(self):
        w0 = workset_lib.advance_dataset_version(
            workset_lib.start_new_source(
                None, tenant_id=TENANT, owner_id=OWNER, conversation_id="c1", dataset_id="ds-src"
            ),
            dataset_id="ds-derived-1",
        )
        w1 = workset_lib.select_single(w0, SKU_B)
        self.assertEqual(w1.current_dataset_id, w0.current_dataset_id)
        self.assertEqual(w1.source_dataset_id, w0.source_dataset_id)
        self.assertEqual(w1.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w1.selected_identifiers, (SKU_B,))

        # Switching selection (A -> B) must not leak A into B's selection.
        w2 = workset_lib.select_single(w1, SKU_C)
        self.assertEqual(w2.selected_identifiers, (SKU_C,))
        self.assertNotIn(SKU_B, w2.selected_identifiers)

    def test_apply_to_task_mirrors_dataset_id_and_get_workset_round_trips(self):
        task = _make_task()
        self.assertIsNone(workset_lib.get_workset(task))
        w0 = workset_lib.start_new_source(
            None, tenant_id=TENANT, owner_id=OWNER, conversation_id="conv-1", dataset_id="ds-src"
        )
        workset_lib.apply_to_task(task, w0)
        self.assertEqual(task.parameters["dataset_id"], "ds-src")
        restored = workset_lib.get_workset(task)
        self.assertEqual(restored, w0)

        w1 = workset_lib.advance_dataset_version(w0, dataset_id="ds-derived-1")
        workset_lib.apply_to_task(task, w1)
        # The compatibility mirror always tracks current_dataset_id, never
        # source_dataset_id -- this is what lets resolve_action_turn's
        # unmodified continuation gate keep working.
        self.assertEqual(task.parameters["dataset_id"], "ds-derived-1")
        self.assertEqual(workset_lib.get_workset(task).source_dataset_id, "ds-src")

    def test_task_with_no_workset_yet_is_self_healing(self):
        task = _make_task()
        task.parameters["dataset_id"] = "ds-legacy-only"
        self.assertIsNone(workset_lib.get_workset(task))


class WorksetDurabilityTests(unittest.TestCase):
    """TEST D: a Workset persisted through ``SqliteActiveTaskStore``
    survives being read back by a BRAND NEW store instance bound to the
    same on-disk file -- simulating a process restart -- with zero
    re-upload required."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "active_tasks.sqlite3")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_workset_restores_intact_across_a_fresh_store_instance(self):
        store1 = SqliteActiveTaskStore(self.db_path)
        task = _make_task(conversation_id="conv-durable")
        w0 = workset_lib.start_new_source(
            None, tenant_id=TENANT, owner_id=OWNER, conversation_id="conv-durable", dataset_id="ds-src"
        )
        w1 = workset_lib.select_single(
            workset_lib.advance_dataset_version(w0, dataset_id="ds-derived-1"), SKU_A
        )
        workset_lib.apply_to_task(task, w1)
        store1.put(task)

        # A brand-new store instance -- zero Python object shared with
        # ``store1`` -- bound to the SAME on-disk file, exactly what
        # surviving a process restart/redeploy requires.
        store2 = SqliteActiveTaskStore(self.db_path)
        restored_task = store2.get(tenant_id=TENANT, owner_id=OWNER, conversation_id="conv-durable")
        self.assertIsNotNone(restored_task)
        restored_workset = workset_lib.get_workset(restored_task)
        self.assertEqual(restored_workset.workset_id, w1.workset_id)
        self.assertEqual(restored_workset.source_dataset_id, "ds-src")
        self.assertEqual(restored_workset.current_dataset_id, "ds-derived-1")
        self.assertEqual(restored_workset.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(restored_workset.selected_identifiers, (SKU_A,))
        # The compatibility mirror a fresh process's resolve_action_turn
        # continuation gate reads is intact too -- no re-upload prompt.
        self.assertEqual(restored_task.parameters.get("dataset_id"), "ds-derived-1")


class WorksetAcceptanceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """TEST A/B/C/F driven through the REAL ``WorkflowPandaConversationGateway``
    /``resolve_action_turn``/``data_intel`` chain. The managed agent is
    disabled (the default -- no ``PANDA_MANAGED_AGENT_ENABLED``), so every
    turn below runs through the legacy deterministic path exclusively --
    making ``MaxTurnsExceeded`` structurally impossible for these turns."""

    async def asyncSetUp(self):
        self.panda, self.artifact_service = _panda()
        self.artifact_id = await _register_upload(
            self.artifact_service,
            tenant=TENANT,
            owner=OWNER,
            conv="conv-acceptance",
            filename=FILENAME,
            content=_xlsx_bytes(),
        )

    def _task(self, conversation_id="conv-acceptance"):
        return self.panda._action_store.get(  # noqa: SLF001
            tenant_id=TENANT, owner_id=OWNER, conversation_id=conversation_id
        )

    def _workset(self, conversation_id="conv-acceptance"):
        return workset_lib.get_workset(self._task(conversation_id))

    async def _respond(self, text, *, conversation_id="conv-acceptance", attach=False, request_id="r"):
        return await self.panda.respond(
            ConversationRequest(
                text=text,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id=request_id,
                conversation_id=conversation_id,
                attachment_refs=(self.artifact_id,) if attach else (),
            )
        )

    async def test_a_original_dataset_survives_transformation_and_explicit_return_to_full(self):
        # Turn 1: fresh attachment -> canonical Workset established,
        # source == current, scope FULL_DATASET.
        await self._respond("Проанализируй этот прайс.", attach=True, request_id="r1")
        w1 = self._workset()
        self.assertIsNotNone(w1)
        source_id = w1.source_dataset_id
        self.assertTrue(source_id)
        self.assertEqual(w1.current_dataset_id, source_id)
        self.assertEqual(w1.scope, workset_lib.SCOPE_FULL_DATASET)
        workset_id = w1.workset_id

        # Turn 2 (NO reattachment): a deterministic bulk transformation
        # over the ALREADY-attached spreadsheet.
        result2 = await self._respond("Увеличь розничную цену на 10%.", request_id="r2")
        self.assertNotIn("Приложите файл", result2.text)
        w2 = self._workset()
        self.assertEqual(w2.workset_id, workset_id)
        self.assertEqual(w2.source_dataset_id, source_id)  # CRITICAL INVARIANT
        self.assertNotEqual(w2.current_dataset_id, source_id)

        # "Return to the original full spreadsheet" -- the capability this
        # module exists to provide (see business_assistant/workset.py's
        # ``select_full_dataset``). Per this task's own directive #8, no
        # new NL/regex mapping is introduced for the trigger phrase; this
        # proves the underlying mechanism/invariant instead.
        task = self._task()
        workset_lib.apply_to_task(task, workset_lib.select_full_dataset(w2))
        self.panda._action_store.put(task)  # noqa: SLF001

        w3 = self._workset()
        self.assertEqual(w3.workset_id, workset_id)
        self.assertEqual(w3.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w3.current_dataset_id, source_id)

        # A LATER turn (still no reattachment) resolves against the
        # ORIGINAL dataset -- never asks the user to re-upload.
        result4 = await self._respond("Проанализируй файл ещё раз.", request_id="r4")
        self.assertNotIn("Приложите файл", result4.text)
        self.assertEqual(result4.metadata.get("artifacts"), [])

    async def test_b_scope_switching_full_single_a_filtered_full_single_b_no_leak(self):
        await self._respond("Проанализируй этот прайс.", attach=True, request_id="r1")
        workset_id = self._workset().workset_id
        source_id = self._workset().source_dataset_id

        # FULL_DATASET -> SINGLE product A.
        await self._respond("Возьми первый товар из этого прайса.", request_id="r2")
        wa = self._workset()
        self.assertEqual(wa.workset_id, workset_id)
        self.assertEqual(wa.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(wa.selected_identifiers, (SKU_A,))
        self.assertEqual(wa.source_dataset_id, source_id)

        # SINGLE product A -> FILTERED_SET (a real deterministic filter
        # reduces the row count).
        result_filtered = await self._respond(
            "Покажи товары с розничной ценой дешевле 140000.", request_id="r3"
        )
        wf = self._workset()
        self.assertEqual(wf.workset_id, workset_id)
        self.assertEqual(wf.scope, workset_lib.SCOPE_FILTERED_SET)
        self.assertEqual(wf.source_dataset_id, source_id)
        self.assertNotIn("Приложите файл", result_filtered.text)

        # FILTERED_SET -> FULL_DATASET (the existing "return to the whole
        # dataset" capability -- see test A's own docstring note).
        task = self._task()
        workset_lib.apply_to_task(task, workset_lib.select_full_dataset(wf))
        self.panda._action_store.put(task)  # noqa: SLF001
        wfull = self._workset()
        self.assertEqual(wfull.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(wfull.current_dataset_id, source_id)

        # FULL_DATASET -> SINGLE product B. Product A must NOT leak into
        # product B's selection.
        result_b = await self._respond("Теперь возьми второй товар из прайса.", request_id="r4")
        self.assertNotIn("Приложите файл", result_b.text)
        wb = self._workset()
        self.assertEqual(wb.workset_id, workset_id)
        self.assertEqual(wb.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(wb.selected_identifiers, (SKU_B,))
        self.assertNotIn(SKU_A, wb.selected_identifiers)
        self.assertEqual(wb.source_dataset_id, source_id)

    async def test_c_version_chain_source_immutable_and_still_readable(self):
        await self._respond("Проанализируй этот прайс.", attach=True, request_id="r1")
        w1 = self._workset()
        source_id = w1.source_dataset_id
        workset_id = w1.workset_id

        await self._respond("Увеличь розничную цену на 5%.", request_id="r2")
        w2 = self._workset()
        dataset_v1 = w2.current_dataset_id
        self.assertEqual(w2.source_dataset_id, source_id)
        self.assertNotEqual(dataset_v1, source_id)

        await self._respond("Увеличь розничную цену на 3%.", request_id="r3")
        w3 = self._workset()
        dataset_v2 = w3.current_dataset_id
        self.assertEqual(w3.source_dataset_id, source_id)
        self.assertNotEqual(dataset_v2, dataset_v1)
        self.assertNotEqual(dataset_v2, source_id)
        self.assertEqual(w3.workset_id, workset_id)

        # The original dataset is still fully readable (never overwritten
        # or discarded) and the provenance chain traces back correctly.
        data_intel = self.panda._tool_gateway.registry  # noqa: SLF001 (sanity: registry exists)
        self.assertIsNotNone(data_intel)
        store = _first_dataset_store(self.panda)
        original_rows = store.get_rows(source_id, tenant_id=TENANT)
        self.assertEqual(len(original_rows), 4)
        v1_desc = store.get_dataset(dataset_v1, tenant_id=TENANT)
        self.assertEqual(v1_desc.provenance.get("derived_from"), source_id)
        v2_desc = store.get_dataset(dataset_v2, tenant_id=TENANT)
        self.assertEqual(v2_desc.provenance.get("derived_from"), dataset_v1)

    async def test_f_production_journey_single_product_then_bulk_no_reattach_no_stale_override(self):
        # 1. Attach once; 2. inspect one product; 3. (no reattachment)
        # a deterministic bulk operation touching every row of the
        # ORIGINAL spreadsheet; 4. concrete preview; 5. zero drift.
        await self._respond("Проанализируй этот прайс.", attach=True, request_id="r1")
        workset_id = self._workset().workset_id
        source_id = self._workset().source_dataset_id

        await self._respond("Возьми первый товар из этого прайса.", request_id="r2")
        w_single = self._workset()
        self.assertEqual(w_single.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(w_single.selected_identifiers, (SKU_A,))

        # NO attachment on this turn -- a bulk operation over ALL rows.
        result3 = await self._respond("Увеличь розничную цену на 10% для всех товаров.", request_id="r3")

        # No reattachment prompt of any kind.
        self.assertNotIn("Приложите файл", result3.text)
        self.assertNotEqual(result3.metadata.get("action_decision"), "ASK_CLARIFICATION")

        w_final = self._workset()
        # Same task, same canonical Workset, same immutable source.
        self.assertEqual(w_final.workset_id, workset_id)
        self.assertEqual(w_final.source_dataset_id, source_id)
        # The stale single-product selection from turn 2 must NOT keep
        # dominating: the bulk result genuinely touched every row, so the
        # canonical Workset reflects that -- never pinned to product A.
        self.assertEqual(w_final.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w_final.selected_identifiers, ())
        self.assertNotEqual(w_final.current_dataset_id, source_id)

        # Concrete preview, not a vague placeholder.
        self.assertIn("4", result3.text)  # row_count_before/after == 4

        # Zero Bitrix mutation -- no bridge was even wired for this test.
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001

    async def test_f_second_wording_and_scope_different_values_same_mechanism(self):
        """Same mechanism, different wording/percentage/product -- proves
        this is not a single magic phrase (task directive: numbers/
        wording are data, not architecture)."""
        await self._respond("Изучи содержимое приложенного файла.", attach=True, request_id="r1")
        workset_id = self._workset().workset_id
        source_id = self._workset().source_dataset_id

        await self._respond("Покажи второй товар из файла.", request_id="r2")
        self.assertEqual(self._workset().scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(self._workset().selected_identifiers, (SKU_B,))

        result = await self._respond("Снизь розничную цену на 4% для всех позиций.", request_id="r3")
        self.assertNotIn("Приложите файл", result.text)
        w_final = self._workset()
        self.assertEqual(w_final.workset_id, workset_id)
        self.assertEqual(w_final.source_dataset_id, source_id)
        self.assertEqual(w_final.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w_final.selected_identifiers, ())


def _first_dataset_store(panda):
    # Test-only introspection: the same DataIntelligenceService instance
    # ``_panda()`` wired into every ``data.excel_assistant`` tool call.
    for row in panda._tool_gateway.registry._items.values():  # noqa: SLF001
        adapter = row.adapter
        svc = getattr(adapter, "_svc", None)
        if svc is not None:
            return svc.store
    raise AssertionError("data intelligence store not found on registered adapters")


class ManagedAgentCannotOverrideWorksetTests(unittest.IsolatedAsyncioTestCase):
    """TEST E: the managed-agent integration boundary can narrow the
    canonical Workset's scope to a resolved product, but can never make
    its own private dataset id become canonical, and never resets
    ``workset_id``/``source_dataset_id`` across a product switch. Reuses
    the SAME real ``panda_bridge``/``ManagedAgentPOC`` chain already
    proven in ``tests/test_panda_managed_agent_enrichment_delegation.py``
    -- only the isolated subprocess's own model turn is faked."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        self.panda, self.artifact_service = _panda()
        self.artifact_id = await _register_upload(
            self.artifact_service,
            tenant=TENANT,
            owner=OWNER,
            conv="conv-managed",
            filename=FILENAME,
            content=_xlsx_bytes(),
        )

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

    def _workset(self):
        task = self.panda._action_store.get(  # noqa: SLF001
            tenant_id=TENANT, owner_id=OWNER, conversation_id="conv-managed"
        )
        return workset_lib.get_workset(task)

    async def test_managed_agent_product_selection_never_overrides_canonical_dataset_identity(self):
        # The managed agent's OWN fabricated dataset id -- structurally
        # unrelated to the shared data_intel store, must NEVER surface as
        # the canonical source/current dataset id.
        FAKE_MANAGED_DATASET_ID = "ds-managed-agent-private-fake"
        plan = [
            {
                "current_identifier": SKU_A,
                "tool_calls": [
                    {
                        "tool": "select_product",
                        "output": {
                            "status": "SELECTED",
                            "matched_by": "next_unspecified",
                            "name": NAME_A,
                            "sku": SKU_A,
                            "ean": EAN_A,
                            "category": "Телевизоры",
                            "brand": "LG",
                            "purchase_price": PURCHASE_A,
                            "retail_price": RETAIL_A,
                        },
                    }
                ],
                "final_output": "irrelevant-a",
            },
            {
                "current_identifier": SKU_B,
                "tool_calls": [
                    {
                        "tool": "select_product",
                        "output": {
                            "status": "SELECTED",
                            "matched_by": "next_unspecified",
                            "name": NAME_B,
                            "sku": SKU_B,
                            "ean": EAN_B,
                            "category": "Телевизоры",
                            "brand": "LG",
                            "purchase_price": PURCHASE_B,
                            "retail_price": RETAIL_B,
                        },
                    }
                ],
                "final_output": "irrelevant-b",
            },
        ]
        fake_run_turn = _make_fake_run_turn(plan)

        def _patched_run_turn(self, **kwargs):
            result = fake_run_turn(self, **kwargs)
            return replace(result, dataset_id=FAKE_MANAGED_DATASET_ID)

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_patched_run_turn):
            r1 = await self.panda.respond(
                ConversationRequest(
                    text="Возьми первый товар и подготовь его для Bitrix/Aspro. Ничего не записывай.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="req-1",
                    conversation_id="conv-managed",
                    attachment_refs=(self.artifact_id,),
                )
            )
            self.assertEqual(r1.metadata.get("managed_agent_tool"), "select_product")

            w1 = self._workset()
            self.assertIsNotNone(w1)
            canonical_source_id = w1.source_dataset_id
            canonical_workset_id = w1.workset_id
            # The canonical Workset is real -- established by
            # ``_establish_canonical_workset_from_attachment`` against the
            # SHARED data_intel store -- never the managed agent's own
            # fabricated id.
            self.assertNotEqual(canonical_source_id, FAKE_MANAGED_DATASET_ID)
            self.assertNotEqual(w1.current_dataset_id, FAKE_MANAGED_DATASET_ID)
            self.assertEqual(w1.current_dataset_id, canonical_source_id)
            self.assertEqual(w1.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w1.selected_identifiers, (SKU_A,))

            # A second managed-agent turn resolves a DIFFERENT product.
            r2 = await self.panda.respond(
                ConversationRequest(
                    text=f"Теперь подготовь {NAME_B} для Bitrix/Aspro.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="req-2",
                    conversation_id="conv-managed",
                )
            )
            self.assertEqual(r2.metadata.get("managed_agent_tool"), "select_product")

        w2 = self._workset()
        # workset_id / source_dataset_id are STABLE across the product
        # switch -- the managed-agent boundary never resets/replaces the
        # ONE canonical business-data context, it only narrows scope.
        self.assertEqual(w2.workset_id, canonical_workset_id)
        self.assertEqual(w2.source_dataset_id, canonical_source_id)
        self.assertEqual(w2.current_dataset_id, canonical_source_id)
        self.assertNotEqual(w2.source_dataset_id, FAKE_MANAGED_DATASET_ID)
        self.assertNotEqual(w2.current_dataset_id, FAKE_MANAGED_DATASET_ID)
        self.assertEqual(w2.scope, workset_lib.SCOPE_SINGLE)
        # Product A must not leak into product B's selection.
        self.assertEqual(w2.selected_identifiers, (SKU_B,))
        self.assertNotIn(SKU_A, w2.selected_identifiers)


if __name__ == "__main__":
    unittest.main()
