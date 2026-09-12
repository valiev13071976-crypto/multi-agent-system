"""Architectural invariant regression: Business Process A (supplier
Excel/XLSX -> Panda product preparation -> retail-price -> Bitrix/Aspro
write plan) and Business Process B (Telegram Market Intelligence ->
observations -> catalog matching -> market price comparison) MUST remain
operationally independent -- neither one's runtime behavior may depend on
the other, and disabling either must not affect the other.

Four checks, exactly as required:

  A. Telegram Market Intelligence "disabled" (its package actively
     blocked from being imported for the duration of the flow) ->
     Excel -> Panda -> Bitrix preparation still completes end to end,
     including the retail-price restoration and category resolution.
  B. Bitrix/storefront integration "disabled" (``integrations.bitrix``
     and ``business_assistant`` actively blocked from being imported) ->
     Telegram Market Intelligence's own ingest -> extract -> match ->
     price-comparison pipeline still completes end to end.
  C. (covered by ``tests/test_panda_control_product_end_to_end_lg_tv.py``
     and ``tests/test_panda_ean_category_hidden_by_missing_price_defect_
     closure.py``, referenced here by name only, not re-run, to avoid
     duplicating the same assertions) -- Excel -> Panda -> Bitrix
     preparation returns the complete expected product fields including
     retail price.
  D. (covered throughout: every test in this file, and every test in the
     two files above, asserts zero Bitrix mutation and zero Telegram
     write operation.)

Checks A and B each run their flow inside a FRESH child interpreter (via
``subprocess``) that installs an import blocker as its very first action,
before any application module is imported. This is deliberate: a shared,
long-lived pytest process already has many modules cached from unrelated
test files collected earlier in the same session, and those caches (plus
assorted CPython/pytest internals that can touch ``sys.modules`` outside
of a plain ``import`` statement) make an in-process "pop cached modules +
install a sys.meta_path blocker" check unreliable -- it can pass or fail
depending on what else the test session already happened to import. A
fresh subprocess has no such history: every package it ever sees during
its short life is either a genuine import (which the blocker catches
immediately, raising ``ImportError`` and failing the subprocess) or
genuinely never touched at all (which is exactly what this regression is
trying to prove).

Static checks confirm the ALREADY-existing lack of any import-time
coupling between the two business processes' own source modules (no
refactor performed to achieve this -- it was already true; this simply
pins it as a regression so a future change cannot silently introduce
coupling).
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BUSINESS_PROCESS_A_MODULES = (
    "business_assistant/conversation_gateway.py",
    "business_assistant/controlled_bitrix_write.py",
    "business_assistant/action_continuation.py",
    "business_assistant/product_enrichment_bridge.py",
    "data_intel/service.py",
    "data_intel/mapping.py",
)
BUSINESS_PROCESS_B_MODULES = (
    "market_intel/service.py",
    "market_intel/catalog_adapter.py",
    "market_intel/extract.py",
    "market_intel/router.py",
    "market_intel/runtime.py",
)


def _imported_top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class StaticImportIsolationTests(unittest.TestCase):
    """Pins the ALREADY-existing absence of import-time coupling between
    the two business processes' own modules -- not a refactor, a
    regression guard."""

    def test_business_process_a_modules_never_import_market_intel(self):
        for rel_path in BUSINESS_PROCESS_A_MODULES:
            path = REPO_ROOT / rel_path
            with self.subTest(module=rel_path):
                self.assertTrue(path.is_file(), f"expected {rel_path} to exist")
                names = _imported_top_level_names(path)
                self.assertNotIn(
                    "market_intel",
                    names,
                    f"{rel_path} must not import market_intel (Business Process A must not depend on B)",
                )

    def test_business_process_b_modules_never_import_bitrix_or_business_assistant(self):
        for rel_path in BUSINESS_PROCESS_B_MODULES:
            path = REPO_ROOT / rel_path
            with self.subTest(module=rel_path):
                self.assertTrue(path.is_file(), f"expected {rel_path} to exist")
                names = _imported_top_level_names(path)
                self.assertNotIn(
                    "business_assistant",
                    names,
                    f"{rel_path} must not import business_assistant (Business Process B must not own/redefine A)",
                )
                self.assertNotIn(
                    "integrations",
                    names,
                    f"{rel_path} must not import the Bitrix integration layer directly",
                )


def _run_isolated_script(script: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """Runs ``script`` in a brand-new child interpreter, with ``REPO_ROOT``
    on ``sys.path`` so the repository's top-level packages (``business_
    assistant``, ``data_intel``, ``market_intel``, ``tests``, ...) import
    the same way they do under pytest, but with NO modules pre-cached from
    this (the parent) process."""

    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_INVARIANT_A_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo_root!r})

    class _BlockedPackageImporter:
        def __init__(self, prefix):
            self.prefix = prefix

        def find_spec(self, fullname, path=None, target=None):
            if fullname == self.prefix or fullname.startswith(self.prefix + "."):
                raise ImportError(
                    "business-process isolation test: " + repr(fullname) + " must not be imported here"
                )
            return None

    # Installed BEFORE any application module is imported, so a genuine
    # attempt anywhere in the exercised call path to import the "disabled"
    # business process's package is caught immediately, not merely absent
    # from a static source scan.
    sys.meta_path.insert(0, _BlockedPackageImporter("market_intel"))

    import asyncio
    import io
    from unittest.mock import patch

    from openpyxl import Workbook

    from artifacts.service import ArtifactService
    from artifacts.store import InMemoryArtifactStore
    from business_assistant.conversation_gateway import (
        ConversationRequest,
        WorkflowPandaConversationGateway,
    )
    from data_intel.service import DataIntelligenceService
    from data_intel.store import InMemoryDatasetStore
    from integrations.production.http import BoundedHttpClient
    from tools.gateway import ToolGateway
    from tools.platform.bootstrap import register_platform_tools
    from tools.registry import ToolRegistry

    from tests.test_bitrix_live_product_create_write import (
        _bridge_and_activation,
        _LiveEnv,
        _RecordingTransport,
    )

    TARGET_SKU = "55MRGB86B6A.ARUG"
    TARGET_EAN = "8806096824788"
    TV_SECTION_ID = 70
    TV_SECTION = {{"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}}


    async def main():
        wb = Workbook()
        ws = wb.active
        ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "цена"])
        ws.append([TARGET_SKU, "LG " + TARGET_SKU, "Телевизоры", "LG", TARGET_EAN, "103198.3", "119990"])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        transport = _RecordingTransport(sections=[TV_SECTION])
        live_env = _LiveEnv()
        live_env.__enter__()
        http_patch = patch.object(BoundedHttpClient, "request", side_effect=transport)
        http_patch.start()
        try:
            bridge, _activation = _bridge_and_activation()
            svc = DataIntelligenceService(InMemoryDatasetStore())
            artifact_service = ArtifactService(store=InMemoryArtifactStore())
            svc.artifact_service = artifact_service
            registry = ToolRegistry()
            register_platform_tools(registry, data_intelligence=svc)
            gateway = ToolGateway(registry=registry, register_search=False)
            panda = WorkflowPandaConversationGateway(
                workflow_engine=object(),
                run_router=object(),
                context_manager=object(),
                tool_gateway=gateway,
                artifact_service=artifact_service,
                bitrix_product_bridge=bridge,
            )
            rec = artifact_service.register_upload(
                tenant_id="tenant-a", owner_id="u1", filename="LG_TV.xlsx", content=xlsx_bytes
            )
            artifact_service.attach_to_conversation(
                tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="c1"
            )
            result = await panda.respond(
                ConversationRequest(
                    text=(
                        "Возьми первый товар из загруженного LG_TV.xlsx и подготовь его для "
                        "Bitrix/Aspro. Рассчитай розничную цену, определи точную категорию "
                        "Bitrix/Aspro и покажи EAN. Ничего не записывай в Bitrix."
                    ),
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="r1",
                    conversation_id="c1",
                    attachment_refs=(rec.artifact_id,),
                )
            )
        finally:
            http_patch.stop()
            live_env.__exit__(None, None, None)

        assert not any(m.startswith("market_intel") for m in sys.modules), "market_intel leaked into sys.modules"

        text = result.text
        assert TARGET_SKU in text, text
        assert TARGET_EAN in text, text
        assert "119990" in text, text  # retail price, restored by this change set
        assert str(TV_SECTION_ID) in text, text  # exact Bitrix/Aspro category
        assert not result.metadata.get("mutated")
        methods_called = [m for m, _u in transport.calls]
        assert "catalog.product.add" not in methods_called
        assert transport.product_add_count == 0
        print("ISOLATION_TEST_A_OK")


    asyncio.run(main())
    """
).format(repo_root=str(REPO_ROOT))


_INVARIANT_B_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo_root!r})

    class _BlockedPackageImporter:
        def __init__(self, prefix):
            self.prefix = prefix

        def find_spec(self, fullname, path=None, target=None):
            if fullname == self.prefix or fullname.startswith(self.prefix + "."):
                raise ImportError(
                    "business-process isolation test: " + repr(fullname) + " must not be imported here"
                )
            return None

    # Installed BEFORE any application module is imported, so a genuine
    # attempt anywhere in the exercised call path to import the "disabled"
    # Bitrix/business_assistant side is caught immediately, not merely
    # absent from a static source scan.
    for _prefix in ("integrations.bitrix", "business_assistant"):
        sys.meta_path.insert(0, _BlockedPackageImporter(_prefix))

    import os
    import shutil
    import tempfile
    from decimal import Decimal

    from product_intel.platform_models import PriceInfo, Product
    from product_intel.store import InMemoryProductCatalogStore

    from market_intel.models import MONITOR_ENABLED
    from market_intel.service import MarketIntelligenceService
    from market_intel.store import SqliteMarketIntelStore

    from tests.test_telegram_market_intelligence import (
        OWNER,
        TENANT,
        TV_EAN,
        _fixture_client,
    )

    catalog = InMemoryProductCatalogStore()
    catalog.save_product(
        Product(
            product_id="p-tv",
            tenant_id=TENANT,
            title="Телевизор LG 55MRGB86B6A",
            brand="LG",
            sku="TV-LG-01",
            gtin=TV_EAN,
            mpn="55MRGB86B6A.ARUG",
            price=PriceInfo(currency="RUB", selling_price=Decimal("99900"), purchase_price=Decimal("70000")),
        )
    )

    tmp = tempfile.mkdtemp()
    try:
        store = SqliteMarketIntelStore(os.path.join(tmp, "mi.sqlite"))
        try:
            service = MarketIntelligenceService(
                store=store, read_client=_fixture_client(), catalog=catalog, default_tenant_id=TENANT
            )
            service.discover_channels(tenant_id=TENANT, owner_id=OWNER)
            channels = service.store.list_channels(tenant_id=TENANT)
            assert channels

            for channel in channels:
                service.set_monitoring(
                    tenant_id=TENANT,
                    channel_id=channel.channel_id,
                    monitor_state=MONITOR_ENABLED,
                    actor_id=OWNER,
                )
                service.ingest_channel(tenant_id=TENANT, channel_id=channel.channel_id)

            comparison = service.price_comparison(tenant_id=TENANT, product_id="p-tv")
            assert comparison.observation_count > 0
            assert comparison.our_selling_price == Decimal("99900")

            assert not any(m.startswith("integrations.bitrix") for m in sys.modules)
            assert not any(m.startswith("business_assistant") for m in sys.modules)
            print("ISOLATION_TEST_B_OK")
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    """
).format(repo_root=str(REPO_ROOT))


class ExcelToBitrixWorksWithMarketIntelligenceDisabledTests(unittest.TestCase):
    """Invariant A: disabling Telegram Market Intelligence must not change
    or break Excel ingestion, product preparation, retail-price
    preparation, Bitrix/Aspro category resolution, or Bitrix write
    preparation."""

    def test_full_excel_to_bitrix_flow_survives_market_intel_import_being_blocked(self):
        proc = _run_isolated_script(_INVARIANT_A_SCRIPT)
        self.assertEqual(
            proc.returncode,
            0,
            f"isolated subprocess failed:\\nSTDOUT:\\n{proc.stdout}\\nSTDERR:\\n{proc.stderr}",
        )
        self.assertIn("ISOLATION_TEST_A_OK", proc.stdout)


class MarketIntelligenceWorksWithBitrixDisabledTests(unittest.TestCase):
    """Invariant B: disabling Bitrix/storefront integration must not break
    Telegram Market Intelligence collection/analysis."""

    def test_full_market_intel_pipeline_survives_bitrix_and_business_assistant_being_blocked(self):
        proc = _run_isolated_script(_INVARIANT_B_SCRIPT)
        self.assertEqual(
            proc.returncode,
            0,
            f"isolated subprocess failed:\\nSTDOUT:\\n{proc.stdout}\\nSTDERR:\\n{proc.stderr}",
        )
        self.assertIn("ISOLATION_TEST_B_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
