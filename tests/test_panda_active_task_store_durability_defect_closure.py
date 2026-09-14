"""PANDA — PRODUCTION REGRESSION CLOSURE: PR #71 passed its 6-turn E2E test
but the SAME defect (attachment-less follow-up misrouted into the legacy
``BA_CAPABILITY_UNAVAILABLE`` / ``dependency_not_ready`` path) reappeared in
real production immediately after #71 was deployed.

==================================================
PROVEN ROOT CAUSE (the exact #71-test vs. production difference)
==================================================

PR #71's routing fix itself (``WorkflowPandaConversationGateway.
has_active_product_context`` consulted by ``BusinessAssistantService.
submit_request`` before ``classify_intent``/``is_conversational``) is
correct and unchanged by this closure -- ``tests/
test_panda_product_context_continuity_defect_closure.py`` still proves it
end-to-end. The bug this file closes is ONE LAYER BELOW that: WHERE the
active-task state those functions consult actually lives.

``business_assistant.action_continuation.ActiveTaskStore`` -- the ONLY
store ``WorkflowPandaConversationGateway`` ever used in production -- is a
plain process-local ``dict`` (see its own docstring). Nothing in
``main.py``/``business_assistant_api.runtime.wire_panda_conversation_
gateway`` ever passed it a durable ``action_store=``, so production always
took that in-memory default -- UNLIKE every other piece of state this same
multi-turn task depends on, which already IS durable in production:

- the parsed XLSX dataset (``data_intel`` defaults to a SQLite-backed
  ``SqliteDatasetStore``, shared with ``side_effect_runtime.persistence``
  when ready -- see ``data_intel/runtime.py``);
- the conversation/message/request history (``business_assistant_api``'s
  own ``SqliteBusinessAssistantApiStore``);
- uploaded file attachments (``ArtifactService`` over ``SqliteArtifactStore``).

A Railway deploy (exactly what happens right after merging a routing fix
like #71) restarts the ``uvicorn main:app`` process. That restart:

- keeps every durable SQLite-backed piece of state above intact for any
  conversation that was already active before the restart;
- but wipes ``ActiveTaskStore``'s in-memory dict completely.

So a conversation whose turn 1 (attach XLSX, select a product) completed
successfully BEFORE a restart finds, on its very next attachment-less
follow-up turn AFTER that restart, a correctly-computed-but-WRONG answer
from ``has_active_product_context`` (False, because the fresh process's
store is empty) -- reproducing the EXACT pre-#71 symptom
(``BA_CAPABILITY_UNAVAILABLE`` / ``dependency_not_ready`` /
``attachment_count = 0`` / the generic "Задача выполнена..." diagnostic)
even though #71's routing logic itself never regressed.

==================================================
WHY THE #71 6-TURN E2E DID NOT CATCH THIS
==================================================

``ProductContextContinuitySixTurnE2ETests.asyncSetUp`` constructs exactly
ONE ``WorkflowPandaConversationGateway`` (and therefore one in-memory
``ActiveTaskStore``) and runs ALL SIX turns through that SAME instance,
inside ONE Python process, with no teardown/reconstruction between turns.
That is an entirely faithful model of production AS LONG AS the process
never restarts between turns -- which is exactly the condition a real
Railway redeploy violates. A single continuous test run can never exercise
"the process restarted between these two HTTP calls" unless it explicitly
tears down and reconstructs the runtime -- which is exactly what THIS file
does and #71's test structurally could not.

==================================================
THE FIX (durable variant of the SAME store contract, not a new system)
==================================================

``business_assistant.action_continuation.SqliteActiveTaskStore`` implements
the identical ``get``/``put``/``clear`` contract as ``ActiveTaskStore``, but
persists to a dedicated SQLite file instead of a process-local dict --
mirroring the SAME "dedicated SQLite file survives a restart" pattern this
codebase already uses for other in-memory-state-needs-durability problems
(``finops.budget_store.SqliteBudgetStore``,
``providers.governor.SqliteProviderGovernorStore``). ``main.py`` now
constructs one (under the SAME ``PANDA_DATA_DIR`` the existing
``ba_api.sqlite``/``ba_uploads`` already live in) and passes it to
``wire_panda_conversation_gateway(..., action_store=...)``, which forwards
it into ``WorkflowPandaConversationGateway(..., action_store=...)`` --
every existing caller that passes nothing (every other test, including
#71's) keeps the exact prior in-memory default.

==================================================
THIS REGRESSION
==================================================

Unlike #71's test, this file NEVER reuses a single in-process object
across turns. ``_compose_runtime`` performs the REAL production
composition boundary (``build_business_assistant_api_runtime`` +
``wire_panda_conversation_gateway``, wired to dedicated, on-disk SQLite
files for the dataset store, the artifact store, the API's own request/
conversation store, AND the active-task store) and is called TWICE with
the SAME on-disk file paths but ZERO shared Python object references --
simulating turn 1 hitting one process and turn 2 hitting a brand-new one
after a restart/redeploy, exactly what a Railway deploy does. A second
test class proves the OPPOSITE with the old in-memory-only wiring, to
demonstrate the defect this closes actually reproduces without the fix.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import SqliteArtifactStore
from business_assistant.action_continuation import ActiveTaskStore, SqliteActiveTaskStore
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant_api.models import ST_COMPLETED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService
from data_intel.store import SqliteDatasetStore
from integrations.production.http import BoundedHttpClient
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

from tests.test_bitrix_live_product_create_write import (
    _bridge_and_activation,
    _LiveEnv,
    _RecordingTransport,
)

FILENAME = "LG_TV_3PRODUCTS.xlsx"

SKU_A, EAN_A, PRICE_A, RETAIL_A = "TV-A-1001", "4600000000010", "90000", "129990"
SKU_B, EAN_B, PRICE_B, RETAIL_B = "TV-B-2002", "4600000000027", "95000", "139990"
SKU_C, EAN_C, PRICE_C, RETAIL_C = "TV-C-3003", "4600000000034", "99000", "149990"

TV_SECTION = {"id": 70, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# Turn 1: real HTTP request #1 (attachment present) -- attach XLSX, select
# product A. Identical shape to #71's own turn 1.
REQUEST1_TEXT = (
    "Возьми первый товар из этого прайса и подготовь его для Bitrix/Aspro. "
    "Ничего не записывай в Bitrix."
)

# Turn 2: real HTTP request #2, SAME conversation_id, NO attachment --
# natural CHANGE_PRODUCT continuation. Identical shape to #71's own turn 2
# (the exact production symptom: attachment_count == 0, no explicit
# predicate wording, no SKU/EAN/Bitrix word).
REQUEST2_TEXT = "Этот товар уже был. Возьми другой телевизор из прикреплённого прайса."

WORKFLOW_DIAGNOSTIC_MARKERS = (
    "Задача выполнена",
    "Подробности доступны",
    "Requested:",
    "Findings:",
    "Fixture_mode:",
    "Waiting_approval:",
)
GENERIC_STATS_MARKER = "столбцов."


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append([SKU_A, "Модель A", "Телевизоры", "LG", EAN_A, PRICE_A, RETAIL_A])
    ws.append([SKU_B, "Модель B", "Телевизоры", "LG", EAN_B, PRICE_B, RETAIL_B])
    ws.append([SKU_C, "Модель C", "Телевизоры", "LG", EAN_C, PRICE_C, RETAIL_C])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _ComposedProcess:
    """One fully independent runtime composition -- everything a real
    ``uvicorn main:app`` process boots at startup, wired the same way
    ``main.py`` does: durable, dedicated on-disk SQLite files for the
    dataset store, the artifact store, and (the thing under test) the
    active-task store; a brand-new ``ToolRegistry``/``ToolGateway``/
    ``WorkflowPandaConversationGateway``/``BusinessAssistantApiService``
    every single call -- zero Python object shared with any prior call."""

    def __init__(self, *, paths: dict, bridge, durable_action_store: bool):
        self.dataset_store = SqliteDatasetStore(db_path=paths["dataset_db"])
        self.data_intel = DataIntelligenceService(self.dataset_store)
        self.artifact_store = SqliteArtifactStore(paths["artifact_db"])
        self.artifact_service = ArtifactService(store=self.artifact_store)
        self.data_intel.artifact_service = self.artifact_service

        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=self.data_intel)
        self.tool_gateway = ToolGateway(registry=registry, register_search=False)

        # Production regression closure: this is the ONLY store choice this
        # test varies between the two test classes below. ``durable_action_
        # store=True`` reproduces the FIXED ``main.py`` wiring (a
        # ``SqliteActiveTaskStore`` bound to a dedicated on-disk file that
        # both compositions share); ``False`` reproduces the PRE-FIX wiring
        # (``action_store`` left at its in-memory default) to demonstrate
        # the defect actually happens without the fix.
        action_store = SqliteActiveTaskStore(paths["active_task_db"]) if durable_action_store else ActiveTaskStore()
        self.conversation_gateway = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=self.tool_gateway,
            artifact_service=self.artifact_service,
            bitrix_product_bridge=bridge,
            action_store=action_store,
        )
        self.runtime = build_business_assistant_api_runtime(
            db_path=paths["ba_api_db"],
            conversation_gateway=self.conversation_gateway,
            artifact_service=self.artifact_service,
        )
        self.service = self.runtime.service

    def close(self) -> None:
        self.runtime.close()
        try:
            self.dataset_store.close()
        except Exception:
            pass
        try:
            self.artifact_service.close()
        except Exception:
            pass


def _assert_not_degraded(case: unittest.TestCase, summary: str, *, turn_label: str) -> None:
    for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
        case.assertNotIn(
            marker, summary, f"{turn_label} degraded into the legacy generic business workflow (found {marker!r})"
        )


class ActiveTaskStoreDurabilityRestartRegressionTests(unittest.IsolatedAsyncioTestCase):
    """THE mandatory production-composition regression: TWO SEPARATE HTTP
    requests, each through its OWN completely independently constructed
    service/gateway/store composition (never a shared Python object), tied
    together ONLY by the same on-disk SQLite file paths and the same
    ``conversation_id`` -- exactly what surviving a Railway redeploy
    between two real HTTP requests actually requires."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = {
            "ba_api_db": os.path.join(self.tmp, "ba_api.sqlite"),
            "dataset_db": os.path.join(self.tmp, "datasets.sqlite"),
            "artifact_db": os.path.join(self.tmp, "artifacts.sqlite"),
            "active_task_db": os.path.join(self.tmp, "active_tasks.sqlite"),
        }
        self.transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = patch.object(BoundedHttpClient, "request", side_effect=self.transport)
        self.http_patch.start()
        # A dedicated BitrixProductBridge per simulated process (a real
        # restart would also reconstruct this) -- irrelevant to the
        # assertions below since neither request ever confirms a write.
        self.bridge1, _ = _bridge_and_activation()
        self.bridge2, _ = _bridge_and_activation()

    async def asyncTearDown(self):
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_second_request_after_simulated_restart_stays_in_product_workflow(self):
        # ---------------- "PROCESS 1": real HTTP request #1 ----------------
        process1 = _ComposedProcess(paths=self.paths, bridge=self.bridge1, durable_action_store=True)
        rec = process1.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        process1.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-restart-1"
        )
        request1 = process1.service.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=REQUEST1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-restart-1",
            idempotency_key="active-task-durability-request1",
        )
        self.assertEqual(request1.status, ST_COMPLETED)
        result1 = process1.service.get_result(
            tenant_id="tenant-a", owner_id="user-a", request_id=request1.request_id
        )
        summary1 = result1["summary"]
        _assert_not_degraded(self, summary1, turn_label="request1")
        self.assertIn(SKU_A, summary1, "request1 must select product A (first row)")

        # ---------------- SIMULATED RESTART/REDEPLOY ----------------
        # Discard EVERY in-memory Python object from "process 1" -- the
        # gateway, its ActiveTaskStore/SqliteActiveTaskStore connection,
        # the ToolGateway/ToolRegistry, the DataIntelligenceService, the
        # BusinessAssistantApiService -- exactly what a real process exit
        # does. Only the on-disk SQLite files under self.paths survive.
        process1.close()
        del process1

        # ---------------- "PROCESS 2": real HTTP request #2 ----------------
        process2 = _ComposedProcess(paths=self.paths, bridge=self.bridge2, durable_action_store=True)
        request2 = process2.service.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=REQUEST2_TEXT,
            conversation_id="conv-restart-1",
            idempotency_key="active-task-durability-request2",
        )
        self.assertEqual(
            request2.status,
            ST_COMPLETED,
            "request2 (no attachment, after a simulated process restart) must complete through the "
            "conversational pipeline, never BLOCKED by the attachment-blind legacy business workflow",
        )
        result2 = process2.service.get_result(
            tenant_id="tenant-a", owner_id="user-a", request_id=request2.request_id
        )
        summary2 = result2["summary"]
        _assert_not_degraded(self, summary2, turn_label="request2")
        self.assertNotIn(
            GENERIC_STATS_MARKER, summary2, "request2 must not fall back to a generic spreadsheet summary"
        )
        self.assertIn(
            SKU_B,
            summary2,
            "request2 must resolve the existing XLSX/product task from durable state after the restart and "
            "select a DIFFERENT deterministic product (B)",
        )
        self.assertNotIn(SKU_A, summary2, "request2 must not silently keep re-showing product A")

        # Zero real Bitrix mutation across both requests/processes.
        methods_called = [m for m, _ in self.transport.calls]
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)

        process2.close()


class ActiveTaskStoreInMemoryDefaultStillReproducesDefectTests(unittest.IsolatedAsyncioTestCase):
    """Control group: the SAME two-separate-composition scenario above, but
    with ``durable_action_store=False`` (the pre-fix wiring, i.e. every
    caller that never passes ``action_store=`` -- what production actually
    did before this closure). Proves the defect is real and reproducible,
    and that it is specifically the store's process-lifetime that causes
    it (routing/selection logic is otherwise identical and unchanged)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = {
            "ba_api_db": os.path.join(self.tmp, "ba_api.sqlite"),
            "dataset_db": os.path.join(self.tmp, "datasets.sqlite"),
            "artifact_db": os.path.join(self.tmp, "artifacts.sqlite"),
            "active_task_db": os.path.join(self.tmp, "active_tasks.sqlite"),
        }
        self.transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = patch.object(BoundedHttpClient, "request", side_effect=self.transport)
        self.http_patch.start()
        self.bridge1, _ = _bridge_and_activation()
        self.bridge2, _ = _bridge_and_activation()

    async def asyncTearDown(self):
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_without_durable_store_second_request_loses_context_after_restart(self):
        process1 = _ComposedProcess(paths=self.paths, bridge=self.bridge1, durable_action_store=False)
        rec = process1.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        process1.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-restart-2"
        )
        request1 = process1.service.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=REQUEST1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-restart-2",
            idempotency_key="active-task-no-durability-request1",
        )
        self.assertEqual(request1.status, ST_COMPLETED)
        process1.close()
        del process1

        process2 = _ComposedProcess(paths=self.paths, bridge=self.bridge2, durable_action_store=False)
        request2 = process2.service.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=REQUEST2_TEXT,
            conversation_id="conv-restart-2",
            idempotency_key="active-task-no-durability-request2",
        )
        result2 = process2.service.get_result(
            tenant_id="tenant-a", owner_id="user-a", request_id=request2.request_id
        )
        summary2 = result2["summary"]
        # THE REPRODUCED DEFECT: with only the in-memory ActiveTaskStore
        # default (pre-fix production wiring), the restart between request 1
        # and request 2 loses the active task, so request 2 misroutes into
        # the legacy attachment-blind business workflow's generic diagnostic
        # -- the exact real-production symptom this PR closes.
        found_marker = any(marker in summary2 for marker in WORKFLOW_DIAGNOSTIC_MARKERS)
        self.assertTrue(
            found_marker,
            "expected the in-memory-only (pre-fix) wiring to reproduce the degraded generic diagnostic "
            f"after a simulated restart; got: {summary2!r}",
        )
        process2.close()


class SqliteActiveTaskStoreUnitTests(unittest.TestCase):
    """Focused unit pins for the new durable store's contract in isolation
    from the rest of the stack."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "active_tasks.sqlite")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, **overrides):
        from business_assistant.action_continuation import ActiveTask

        base = dict(
            task_id="t1",
            tenant_id="tenant-a",
            owner_id="user-a",
            conversation_id="conv-1",
            family="FAMILY_EXCEL",
            tool_id="data.excel_assistant",
            operation="analyze",
            goal="prepare product",
            parameters={"dataset_id": "ds-1"},
        )
        base.update(overrides)
        return ActiveTask(**base)

    def test_put_then_get_survives_a_fresh_store_instance_same_file(self):
        store1 = SqliteActiveTaskStore(self.path)
        store1.put(self._task())
        # A fresh instance over the SAME file simulates a process restart.
        store2 = SqliteActiveTaskStore(self.path)
        got = store2.get(tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.family, "FAMILY_EXCEL")
        self.assertEqual(got.parameters, {"dataset_id": "ds-1"})

    def test_tenant_and_conversation_isolation(self):
        store = SqliteActiveTaskStore(self.path)
        store.put(self._task())
        self.assertIsNone(store.get(tenant_id="tenant-b", owner_id="user-a", conversation_id="conv-1"))
        self.assertIsNone(store.get(tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-2"))

    def test_put_upserts_the_same_key(self):
        store = SqliteActiveTaskStore(self.path)
        store.put(self._task(status="DRAFT"))
        store.put(self._task(status="READY_TO_EXECUTE"))
        got = store.get(tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1")
        self.assertEqual(got.status, "READY_TO_EXECUTE")

    def test_clear_removes_the_task(self):
        store = SqliteActiveTaskStore(self.path)
        store.put(self._task())
        store.clear(tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1")
        self.assertIsNone(store.get(tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"))


if __name__ == "__main__":
    unittest.main()
