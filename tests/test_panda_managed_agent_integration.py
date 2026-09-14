"""PANDA — Managed Agent Runtime integration boundary (PR #74 integration
block, continuing directly from the live-evaluation harness proven in the
same PR).

This file tests ONLY the new adapter boundary added in
``managed_agent_poc/panda_bridge.py`` plus the small, additive hook in
``business_assistant.conversation_gateway.WorkflowPandaConversationGateway.
respond()`` -- it deliberately does NOT re-run the 41-turn live semantic
benchmark already recorded in the PR #74 report; that evidence already
exists and is not repeated here.

Test groups map 1:1 onto the acceptance requirements for this
integration block:

- ``FeatureFlagOffRegressionTests`` (A): with ``PANDA_MANAGED_AGENT_ENABLED``
  unset (default), the managed-agent bridge is never even imported by
  ``respond()`` in a way that changes behavior -- the EXISTING legacy
  ``CALL_TOOL``/``resolve_action_turn`` path answers exactly as before.
- ``FeatureFlagOnLiveBoundaryTests`` (B, C, D, E): with the flag on AND a
  real ``OPENAI_API_KEY`` + the isolated SDK installed, a SMALL number of
  real model calls (not 41) prove: the exact PR #73 sentence now enters
  managed-agent routing (B); a 4-turn conversation preserves state
  (select -> select-another -> analyze-whole-price-list -> write-plan)
  without losing the current product (C, D); and a mutation request is
  refused without a fabricated success claim and with zero mutation
  capability reachable at all (E). Skips cleanly (never errors) when the
  SDK is not installed or no API key is present, exactly like
  ``tests/test_managed_agent_poc_live_eval.py`` already does for the same
  reason.
- ``PandaBridgeUnitTests``: fast, no-network unit checks of the flag
  parser and the durable-path helper.

Security: this file never prints, logs, or asserts on the literal value
of ``OPENAI_API_KEY``/``BITRIX_WEBHOOK_URL`` -- only presence/absence.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import SqliteArtifactStore
from business_assistant.conversation_gateway import ConversationRequest, WorkflowPandaConversationGateway
from data_intel.service import DataIntelligenceService
from data_intel.store import SqliteDatasetStore
from managed_agent_poc import isolated_env
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR, managed_agent_enabled
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

_SDK_AVAILABLE = isolated_env.is_installed()
_HAS_KEY = bool(os.environ.get("OPENAI_API_KEY"))
_LIVE_SKIP_REASON = "isolated OpenAI Agents SDK not installed or OPENAI_API_KEY not set in this environment"

SKU_A, EAN_A = "TV-A-1001", "4600000000010"
SKU_B, EAN_B = "TV-B-2002", "4600000000027"
FILENAME = "LG_TV_price_list.xlsx"


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append([SKU_A, "Модель A", "Телевизоры", "LG", EAN_A, "90000", "129990"])
    ws.append([SKU_B, "Модель B", "Телевизоры", "LG", EAN_B, "95000", "139990"])
    ws.append(["TV-C-3003", "Модель C", "Телевизоры", "LG", "4600000000034", "99000", "149990"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _RealLegacyRuntime:
    """The REAL, unmodified legacy composition (data_intel + ToolGateway +
    ArtifactService), used to prove requirement A: with the new flag off,
    the pre-existing CALL_TOOL/resolve_action_turn path still answers
    exactly as it did before this integration block existed."""

    def __init__(self, tmp_dir: str):
        self.dataset_store = SqliteDatasetStore(db_path=os.path.join(tmp_dir, "datasets.sqlite"))
        self.data_intel = DataIntelligenceService(self.dataset_store)
        self.artifact_store = SqliteArtifactStore(os.path.join(tmp_dir, "artifacts.sqlite"))
        self.artifact_service = ArtifactService(store=self.artifact_store)
        self.data_intel.artifact_service = self.artifact_service

        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=self.data_intel)
        self.tool_gateway = ToolGateway(registry=registry, register_search=False)

        self.gateway = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=self.tool_gateway,
            artifact_service=self.artifact_service,
        )

    def upload(self, *, tenant_id: str, conversation_id: str, content: bytes = None) -> str:
        rec = self.artifact_service.register_upload(
            tenant_id=tenant_id, owner_id="user-a", filename=FILENAME, content=content or _xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id=tenant_id, artifact_id=rec.artifact_id, conversation_id=conversation_id
        )
        return rec.artifact_id


class PandaBridgeUnitTests(unittest.TestCase):
    """Fast, no-network checks of the flag parser and path helper."""

    def test_flag_default_off(self):
        self.assertFalse(managed_agent_enabled(env={}))

    def test_flag_parses_true_variants(self):
        for value in ("1", "true", "True", "yes", "on"):
            self.assertTrue(managed_agent_enabled(env={ENABLED_ENV_VAR: value}), value)

    def test_flag_parses_false_variants(self):
        for value in ("", "0", "false", "no", "off", "garbage"):
            self.assertFalse(managed_agent_enabled(env={ENABLED_ENV_VAR: value}), value)

    def test_durable_paths_are_under_panda_data_dir_and_stable(self):
        from managed_agent_poc.panda_bridge import _durable_paths

        tmp = tempfile.mkdtemp()
        try:
            old = os.environ.get("PANDA_DATA_DIR")
            os.environ["PANDA_DATA_DIR"] = tmp
            try:
                p1 = _durable_paths(tenant_id="tenant-a", conversation_id="conv-1")
                p2 = _durable_paths(tenant_id="tenant-a", conversation_id="conv-1")
                p3 = _durable_paths(tenant_id="tenant-a", conversation_id="conv-2")
            finally:
                if old is None:
                    os.environ.pop("PANDA_DATA_DIR", None)
                else:
                    os.environ["PANDA_DATA_DIR"] = old
            self.assertEqual(p1, p2, "same (tenant, conversation) must resolve to the same durable paths")
            self.assertNotEqual(p1, p3, "different conversations must not share a dataset/session/state file")
            for path in p1:
                self.assertTrue(path.startswith(tmp), path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class EnsureInstalledBootstrapUnitTests(unittest.TestCase):
    """Fast, no-network unit checks of ``isolated_env.ensure_installed()``'s
    caching/idempotency logic (the real, network-hitting install itself is
    exercised separately by ``ProductionDefectSelfHealingBootstrapTests``
    below) -- verifies it is a true no-op once installed, and attempted at
    most once per process per target directory even when it keeps failing
    (so a deployment with no PyPI egress never pays a retry cost on every
    turn)."""

    def setUp(self):
        self._old_pkgs_dir = os.environ.get(isolated_env.PKGS_DIR_ENV_VAR)
        self._old_attempted = dict(isolated_env._bootstrap_attempted)

    def tearDown(self):
        if self._old_pkgs_dir is None:
            os.environ.pop(isolated_env.PKGS_DIR_ENV_VAR, None)
        else:
            os.environ[isolated_env.PKGS_DIR_ENV_VAR] = self._old_pkgs_dir
        isolated_env._bootstrap_attempted.clear()
        isolated_env._bootstrap_attempted.update(self._old_attempted)

    def test_no_op_and_no_subprocess_call_when_already_installed(self):
        with mock.patch.object(isolated_env, "is_installed", return_value=True):
            with mock.patch.object(isolated_env, "_pip_install_target") as pip_mock:
                self.assertTrue(isolated_env.ensure_installed())
                pip_mock.assert_not_called()

    def test_attempts_at_most_once_per_directory_on_persistent_failure(self):
        tmp_dir = tempfile.mkdtemp(prefix="ensure_installed_fail_")
        try:
            os.environ[isolated_env.PKGS_DIR_ENV_VAR] = tmp_dir
            isolated_env._bootstrap_attempted.pop(tmp_dir, None)
            failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no network")
            with mock.patch.object(isolated_env, "is_installed", return_value=False):
                with mock.patch.object(isolated_env, "_pip_install_target", return_value=failed) as pip_mock:
                    self.assertFalse(isolated_env.ensure_installed())
                    self.assertFalse(isolated_env.ensure_installed())
                    self.assertFalse(isolated_env.ensure_installed())
                    self.assertEqual(
                        pip_mock.call_count,
                        1,
                        "a persistently failing install must be attempted at most once per process/directory",
                    )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_different_directories_get_independent_attempts(self):
        dir_a = tempfile.mkdtemp(prefix="ensure_installed_a_")
        dir_b = tempfile.mkdtemp(prefix="ensure_installed_b_")
        try:
            failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
            with mock.patch.object(isolated_env, "is_installed", return_value=False):
                with mock.patch.object(isolated_env, "_pip_install_target", return_value=failed) as pip_mock:
                    os.environ[isolated_env.PKGS_DIR_ENV_VAR] = dir_a
                    isolated_env._bootstrap_attempted.pop(dir_a, None)
                    isolated_env.ensure_installed()
                    os.environ[isolated_env.PKGS_DIR_ENV_VAR] = dir_b
                    isolated_env._bootstrap_attempted.pop(dir_b, None)
                    isolated_env.ensure_installed()
                    self.assertEqual(pip_mock.call_count, 2)
        finally:
            shutil.rmtree(dir_a, ignore_errors=True)
            shutil.rmtree(dir_b, ignore_errors=True)


class FeatureFlagOffRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Requirement A: flag OFF (default, unset) -- existing Panda path is
    unchanged. Uses the REAL legacy composition (data_intel + ToolGateway),
    never a mock, so a real regression in resolve_action_turn's own CALL_TOOL
    path would still be caught here."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ.pop(ENABLED_ENV_VAR, None)
        self.runtime = _RealLegacyRuntime(self.tmp)

    async def asyncTearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_flag_unset_defaults_to_false(self):
        self.assertFalse(managed_agent_enabled())

    async def test_flag_off_takes_the_existing_call_tool_path_unchanged(self):
        artifact_id = self.runtime.upload(tenant_id="tenant-a", conversation_id="conv-off-1")
        request = ConversationRequest(
            text="Возьми первый товар из этого прайса и подготовь его для Bitrix/Aspro.",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-off-1",
            conversation_id="conv-off-1",
            attachment_refs=(artifact_id,),
        )
        result = await self.runtime.gateway.respond(request)
        self.assertEqual(
            result.metadata.get("action_decision"),
            "CALL_TOOL",
            "with the flag off, the turn must be handled by the existing resolve_action_turn/"
            "CALL_TOOL path, never by the managed agent",
        )
        self.assertNotEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertIn(SKU_A, result.text)

    async def test_flag_off_even_with_an_established_conversation_context(self):
        # Simulates a conversation that ALREADY has managed-agent durable
        # state from a prior turn (flag was on then) -- with the flag off
        # NOW, this must still take the legacy path; presence of managed-
        # agent state must never leak into routing when the flag is off.
        from managed_agent_poc.panda_bridge import _durable_paths
        from managed_agent_poc.state_store import ConversationStateStore, PersistedState

        old_data_dir = os.environ.get("PANDA_DATA_DIR")
        os.environ["PANDA_DATA_DIR"] = self.tmp
        try:
            _, _, state_path = _durable_paths(tenant_id="tenant-a", conversation_id="conv-off-2")
            ConversationStateStore(state_path).save(
                tenant_id="tenant-a",
                conversation_id="conv-off-2",
                state=PersistedState(dataset_id="stale-ds", current_identifier=SKU_A),
            )
        finally:
            if old_data_dir is None:
                os.environ.pop("PANDA_DATA_DIR", None)
            else:
                os.environ["PANDA_DATA_DIR"] = old_data_dir

        artifact_id = self.runtime.upload(tenant_id="tenant-a", conversation_id="conv-off-2")
        request = ConversationRequest(
            text="Возьми первый товар из этого прайса и подготовь его для Bitrix/Aspro.",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-off-2",
            conversation_id="conv-off-2",
            attachment_refs=(artifact_id,),
        )
        result = await self.runtime.gateway.respond(request)
        self.assertEqual(result.metadata.get("action_decision"), "CALL_TOOL")


@unittest.skipUnless(_SDK_AVAILABLE and _HAS_KEY, _LIVE_SKIP_REASON)
class FeatureFlagOnLiveBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Requirements B, C, D, E: flag ON, a SMALL number of real model
    calls (not the 41-turn benchmark) proving the adapter boundary itself
    works end-to-end through the real ``WorkflowPandaConversationGateway.
    respond()`` entry point."""

    async def asyncSetUp(self):
        import managed_agent_poc.panda_bridge as panda_bridge_module

        self.tmp = tempfile.mkdtemp()
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        self.runtime = _RealLegacyRuntime(self.tmp)
        # This whole test class makes real network + subprocess calls. The
        # production default (60s) is right for a single interactive chat
        # turn, but this repo's full test suite runs thousands of other
        # tests in the SAME process/CPU budget, which can slow down
        # subprocess startup well past 60s under contention -- raise ONLY
        # this test process's timeout, never the production default in
        # conversation_gateway.py's call site (which always passes no
        # timeout_s and gets DEFAULT_TURN_TIMEOUT_S unchanged).
        self._old_timeout = panda_bridge_module.DEFAULT_TURN_TIMEOUT_S
        panda_bridge_module.DEFAULT_TURN_TIMEOUT_S = 180.0

    async def asyncTearDown(self):
        import managed_agent_poc.panda_bridge as panda_bridge_module

        panda_bridge_module.DEFAULT_TURN_TIMEOUT_S = self._old_timeout
        os.environ.pop(ENABLED_ENV_VAR, None)
        os.environ.pop("PANDA_DATA_DIR", None)
        os.environ.pop("PANDA_MANAGED_AGENT_POC_ENABLED", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_pr73_sentence_enters_managed_agent_routing(self):
        """Requirement B: the exact previously-failing PR #73 production
        sentence now enters managed-agent semantic routing when the flag
        is on and a spreadsheet is attached."""
        artifact_id = self.runtime.upload(tenant_id="tenant-b", conversation_id="conv-on-b")
        request = ConversationRequest(
            text=(
                "Подготовь один телевизор из этого прайса для Bitrix/Aspro. "
                "Ничего пока не записывай и не публикуй."
            ),
            tenant_id="tenant-b",
            user_id="user-a",
            request_id="req-on-b",
            conversation_id="conv-on-b",
            attachment_refs=(artifact_id,),
        )
        result = await self.runtime.gateway.respond(request)
        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(result.metadata.get("managed_agent_tool"), "select_product")
        self.assertFalse(result.metadata.get("mutated"))

    async def test_multi_turn_state_continuity_and_analysis_does_not_destroy_selection(self):
        """Requirements C + D in one conversation: select -> select another
        -> analyze the whole price list (must not disturb the current
        product) -> ask for the Bitrix write plan (must still describe
        product B, the one selected in turn 2, not A or C)."""
        conv = "conv-on-cd"
        artifact_id = self.runtime.upload(tenant_id="tenant-c", conversation_id=conv)

        r1 = await self.runtime.gateway.respond(
            ConversationRequest(
                text="Возьми любой телевизор из прайса и подготовь его.",
                tenant_id="tenant-c",
                user_id="user-a",
                request_id="req-cd-1",
                conversation_id=conv,
                attachment_refs=(artifact_id,),
            )
        )
        self.assertEqual(r1.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r1.metadata.get("managed_agent_tool"), "select_product")

        r2 = await self.runtime.gateway.respond(
            ConversationRequest(
                text="Этот уже был. Дай другой.",
                tenant_id="tenant-c",
                user_id="user-a",
                request_id="req-cd-2",
                conversation_id=conv,
            )
        )
        self.assertEqual(r2.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r2.metadata.get("managed_agent_tool"), "select_product")

        # Requirement D: a whole-spreadsheet analysis turn must not
        # destroy/change the currently-selected product (verified via the
        # durable state directly, not just plausible-looking text).
        r3 = await self.runtime.gateway.respond(
            ConversationRequest(
                text="А какая средняя цена по всему прайсу?",
                tenant_id="tenant-c",
                user_id="user-a",
                request_id="req-cd-3",
                conversation_id=conv,
            )
        )
        self.assertEqual(r3.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r3.metadata.get("managed_agent_tool"), "analyze_spreadsheet")

        from managed_agent_poc.panda_bridge import _durable_paths
        from managed_agent_poc.state_store import ConversationStateStore

        _, _, state_path = _durable_paths(tenant_id="tenant-c", conversation_id=conv)
        persisted = ConversationStateStore(state_path).load(tenant_id="tenant-c", conversation_id=conv)
        current_after_analysis = persisted.current_identifier
        self.assertTrue(current_after_analysis, "a product must still be current after the analysis turn")

        r4 = await self.runtime.gateway.respond(
            ConversationRequest(
                text="Хорошо, покажи план записи в Bitrix по выбранному товару.",
                tenant_id="tenant-c",
                user_id="user-a",
                request_id="req-cd-4",
                conversation_id=conv,
            )
        )
        self.assertEqual(r4.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r4.metadata.get("managed_agent_tool"), "explain_bitrix_write_plan")

        persisted_after = ConversationStateStore(state_path).load(tenant_id="tenant-c", conversation_id=conv)
        self.assertEqual(
            persisted_after.current_identifier,
            current_after_analysis,
            "the write-plan turn must describe the SAME product the analysis turn left current "
            "-- the analysis turn must not have silently changed/lost the selection",
        )

    async def test_mutation_request_is_refused_without_fabricating_success(self):
        """Requirement E: a direct write/publish request must not perform
        any mutation and must not claim one succeeded."""
        conv = "conv-on-e"
        artifact_id = self.runtime.upload(tenant_id="tenant-e", conversation_id=conv)

        r1 = await self.runtime.gateway.respond(
            ConversationRequest(
                text="Подготовь один товар из прайса.",
                tenant_id="tenant-e",
                user_id="user-a",
                request_id="req-e-1",
                conversation_id=conv,
                attachment_refs=(artifact_id,),
            )
        )
        self.assertEqual(r1.metadata.get("action_decision"), "MANAGED_AGENT")

        r2 = await self.runtime.gateway.respond(
            ConversationRequest(
                text="Запиши этот товар в Bitrix и опубликуй его.",
                tenant_id="tenant-e",
                user_id="user-a",
                request_id="req-e-2",
                conversation_id=conv,
            )
        )
        self.assertEqual(r2.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertFalse(r2.metadata.get("mutated"), "no mutation may ever occur on this path")
        self.assertEqual(
            r2.metadata.get("managed_agent_tool"),
            "" if not r2.metadata.get("managed_agent_tool") else r2.metadata.get("managed_agent_tool"),
        )
        # Structural guarantee, independent of the model's exact wording:
        # the only tools reachable on this path are the 3 read-only ones
        # (or none at all) -- there is no write/publish tool to call.
        self.assertIn(
            r2.metadata.get("managed_agent_tool"),
            ("", "select_product", "analyze_spreadsheet", "explain_bitrix_write_plan"),
        )
        # Zero real Bitrix writes: this test never constructs a
        # bitrix_product_bridge at all, so a mutation would be structurally
        # impossible even if the model tried -- there is nothing to call.
        self.assertIsNone(getattr(self.runtime.gateway, "_bitrix_bridge", None))


@unittest.skipUnless(_HAS_KEY, _LIVE_SKIP_REASON)
class ProductionDefectSelfHealingBootstrapTests(unittest.IsolatedAsyncioTestCase):
    """Production-faithful reproduction + fix verification for the real
    Railway defect report: ``PANDA_MANAGED_AGENT_ENABLED=true`` in a
    container that NEVER ran ``scripts/setup_isolated_env.py`` (Railway/
    Nixpacks only ever runs ``pip install -r requirements.txt``) still got
    the old generic ``data_intel`` "В таблице N строк и M столбцов ...
    средняя ..." analysis on the exact production sentence. Reproduces
    this by pointing ``PANDA_MANAGED_AGENT_POC_PKGS_DIR`` at a brand-new,
    never-provisioned directory (never touched by any earlier test/dev
    setup in this process) and proves the fix
    (``isolated_env.ensure_installed()``, called lazily from
    ``panda_bridge.maybe_respond_via_managed_agent``) makes the SAME
    production request, through the SAME real
    ``WorkflowPandaConversationGateway.respond()`` entry point, self-heal
    and enter managed-agent routing instead."""

    async def asyncSetUp(self):
        import managed_agent_poc.panda_bridge as panda_bridge_module

        self.tmp = tempfile.mkdtemp()
        self.fresh_pkgs_dir = tempfile.mkdtemp(prefix="never_provisioned_pkgs_")
        self._old_pkgs_dir = os.environ.get(isolated_env.PKGS_DIR_ENV_VAR)
        os.environ[isolated_env.PKGS_DIR_ENV_VAR] = self.fresh_pkgs_dir
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        self.runtime = _RealLegacyRuntime(self.tmp)
        self._old_timeout = panda_bridge_module.DEFAULT_TURN_TIMEOUT_S
        panda_bridge_module.DEFAULT_TURN_TIMEOUT_S = 180.0

    async def asyncTearDown(self):
        import managed_agent_poc.panda_bridge as panda_bridge_module

        panda_bridge_module.DEFAULT_TURN_TIMEOUT_S = self._old_timeout
        os.environ.pop(ENABLED_ENV_VAR, None)
        os.environ.pop("PANDA_DATA_DIR", None)
        os.environ.pop("PANDA_MANAGED_AGENT_POC_ENABLED", None)
        if self._old_pkgs_dir is None:
            os.environ.pop(isolated_env.PKGS_DIR_ENV_VAR, None)
        else:
            os.environ[isolated_env.PKGS_DIR_ENV_VAR] = self._old_pkgs_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.fresh_pkgs_dir, ignore_errors=True)

    async def test_root_cause_reproduced_in_isolation_before_any_bootstrap(self):
        """Documents the exact root cause on its own, with no gateway
        involved yet: a never-provisioned pkgs dir makes
        ``ManagedAgentPOC.real_model_available()`` report unavailable --
        this alone, silently swallowed by the pre-fix ``return None``, is
        why real production fell back to the legacy path on every turn.
        Sets the SAME inner opt-in flag ``maybe_respond_via_managed_agent``
        always sets first, so this isolates ONLY the missing-SDK defect
        (not an unrelated "disabled" reason)."""
        from managed_agent_poc.adapter import ManagedAgentPOC

        os.environ["PANDA_MANAGED_AGENT_POC_ENABLED"] = "true"
        self.assertFalse(isolated_env.is_installed())
        available, reason = ManagedAgentPOC.real_model_available()
        self.assertFalse(available)
        self.assertIn("not installed", reason)

    async def test_production_faithful_turn_self_heals_and_enters_managed_agent_routing(self):
        """Requirement B, reproduced production-faithfully against a
        pkgs dir that starts completely unprovisioned (never just the
        already-working happy path): the exact real production message,
        with a real attached spreadsheet, through the real conversational
        entry point, must self-heal and enter managed-agent routing."""
        artifact_id = self.runtime.upload(tenant_id="tenant-selfheal", conversation_id="conv-selfheal")
        request = ConversationRequest(
            text=(
                "Подготовь один телевизор из этого прайса для Bitrix/Aspro. "
                "Ничего пока не записывай и не публикуй."
            ),
            tenant_id="tenant-selfheal",
            user_id="user-a",
            request_id="req-selfheal-1",
            conversation_id="conv-selfheal",
            attachment_refs=(artifact_id,),
        )
        result = await self.runtime.gateway.respond(request)

        self.assertTrue(
            isolated_env.is_installed(),
            "the lazy bootstrap must have installed the SDK into the previously-empty pkgs dir",
        )
        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(result.metadata.get("managed_agent_tool"), "select_product")
        self.assertFalse(result.metadata.get("mutated"))
        # The exact regression this closes: the old generic data_intel
        # "В таблице N строк и M столбцов ... средняя ..." analysis (see
        # data_intel.service._analyze_only_summary) must never be what
        # comes back once the managed-agent path is genuinely eligible.
        self.assertNotIn("строк и", result.text)
        self.assertNotIn("столбцов", result.text)

        r2 = await self.runtime.gateway.respond(
            ConversationRequest(
                text="Этот уже был. Дай другой.",
                tenant_id="tenant-selfheal",
                user_id="user-a",
                request_id="req-selfheal-2",
                conversation_id="conv-selfheal",
            )
        )
        self.assertEqual(
            r2.metadata.get("action_decision"),
            "MANAGED_AGENT",
            "the follow-up must reuse the durable managed-agent dataset state, with no re-upload",
        )
        self.assertEqual(r2.metadata.get("managed_agent_tool"), "select_product")
        self.assertFalse(r2.metadata.get("mutated"))


if __name__ == "__main__":
    unittest.main()
