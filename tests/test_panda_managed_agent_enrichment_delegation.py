"""PANDA — PRODUCTION DEFECT CLOSURE: degraded raw-row product card
returned by the Managed Agent path (``PANDA_MANAGED_AGENT_ENABLED=true``)
after PR #78 already fixed the unbounded recursion in
``resolve_action_turn``.

Reproduced production failure:

    A brand-new Panda chat, ``LG_TV.xlsx`` attached, one message asking
    for a full Bitrix/Aspro product card (retail price, category, main
    image, gallery, announcement, detailed description, TV
    characteristics) for one selected television. The recursion is gone
    (PR #78), but the returned card was degraded/raw-row-like:
      - retail price: "требуется расчёт"
      - main image / gallery / detailed description: absent
      - TV characteristics: almost empty
      - it claimed essentially all requested fields have a confirmed
        Bitrix/Aspro destination (false -- PR #77 marks several fields
        unmapped for the default SIMPLE_PRODUCT model)

Proven root cause (see ``managed_agent_poc/panda_bridge.py``'s own
updated module/function docstrings for the full trace): the isolated
Managed Agent subprocess's 3 tools (``managed_agent_poc.
runtime_subprocess.analyze_spreadsheet`` / ``select_product`` /
``explain_bitrix_write_plan``) are DELIBERATE thin, read-only ROW
PROJECTIONS over the already-ingested spreadsheet -- by design (see that
module's own docstring: "Business rules stay in Panda"), they carry NO
pricing/category/media/characteristics/Bitrix-mapping logic at all. Before
this fix, ``managed_agent_poc.panda_bridge.maybe_respond_via_managed_agent``
answered directly from that raw tool/model output whenever the flag was on
and the turn was eligible -- so a turn handled by the Managed Agent NEVER
reached the EXISTING, already-stabilized
``business_assistant.product_enrichment_bridge.prepare_complete_card`` /
``business_assistant.controlled_bitrix_write.prepare_single_product_write``
pipeline (the SAME one the legacy CALL_PRODUCT_ENRICHMENT/
EXPLAIN_BITRIX_WRITE_PLAN path already uses) at all.

Old broken call path:
    WorkflowPandaConversationGateway.respond()
    -> managed_agent_poc.panda_bridge.maybe_respond_via_managed_agent()
    -> ManagedAgentPOC.run_turn() (isolated subprocess: select_product /
       explain_bitrix_write_plan -- raw row projection only)
    -> raw tool/model output returned AS-IS (no enrichment, no pricing,
       no category resolution, no media, no characteristics, no honest
       mapped/unmapped distinction)

New corrected call path (this change):
    WorkflowPandaConversationGateway.respond()
    -> managed_agent_poc.panda_bridge.maybe_respond_via_managed_agent()
    -> ManagedAgentPOC.run_turn() (UNCHANGED -- still the same 3 tools,
       still raw row projection only)
    -> managed_agent_poc.panda_bridge._selected_product_raw_fields()
       (detects a resolved product from this turn's tool calls)
    -> managed_agent_poc.panda_bridge._delegate_to_existing_product_preparation()
    -> business_assistant.product_enrichment_bridge.prepare_complete_card()
       (EXISTING, UNCHANGED: enrichment + retail-price/category
       resolution via controlled_bitrix_write.prepare_single_product_write
       + honest will_write/will_not_write reporting, PR #77's
       SIMPLE_PRODUCT contract included)
    -> that EXISTING pipeline's own rendered text/write-preview is
       returned to the user instead of the raw tool projection

No new pricing/category/image-search/description/characteristics/Bitrix-
mapping logic was added anywhere -- ``managed_agent_poc/panda_bridge.py``
only translates field NAMES (``_canonical_fields_and_retail_price``) and
calls straight through to the existing capability. ``runtime_subprocess.py``
(the isolated subprocess) is completely untouched. PR #77's SIMPLE_PRODUCT
contract and PR #78's recursion fix are both verified unchanged below.

Zero real Bitrix mutation anywhere in this test: the LIVE bridge is
backed by a mocked HTTP transport that raises on any unexpected call
(including ``catalog.product.add``); no explicit write confirmation is
ever sent, and the Managed Agent path exposes no write/publish tool at
all. Zero live internet: the Bitrix HTTP client, the web-research
fetch tool, and the OpenAI Agents SDK subprocess call are all mocked/
stubbed (``ManagedAgentPOC.run_turn`` is replaced with a deterministic
fake that reproduces the SAME tool-call shapes already proven live in
``tests/test_panda_managed_agent_integration.py`` -- this test asserts
DELEGATION into the existing capabilities, it does not re-implement or
guess their expected business output).
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

from business_assistant.conversation_gateway import ConversationRequest
from integrations.production.http import BoundedHttpClient
from managed_agent_poc.adapter import ManagedAgentPOC, ManagedAgentTurnResult
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR, _durable_paths
from managed_agent_poc.state_store import ConversationStateStore, PersistedState
from product_enrichment.media_fetch import FakeImageFetcher
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tools.search.fake_provider import FakeSearchProvider, fake_result

FILENAME = "LG_TV.xlsx"

PRODUCT_A_SKU = "100MRGB96B6.ARUG"
PRODUCT_A_NAME = "Телевизор LG 100MRGB96B6.ARUG"
PRODUCT_A_EAN = "8806096796849"
PRODUCT_A_PURCHASE_PRICE = "717790.20"
PRODUCT_A_RETAIL_PRICE = "899990"

PRODUCT_B_SKU = "55MRGB86B6A.ARUG"
PRODUCT_B_NAME = "Телевизор LG 55MRGB86B6A.ARUG"
PRODUCT_B_EAN = "8806096824788"
PRODUCT_B_PURCHASE_PRICE = "103198.30"
PRODUCT_B_RETAIL_PRICE = "139990"

CATEGORY = "Телевизоры"
BRAND = "LG"

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# The exact real production first-turn request (LG_TV.xlsx attached).
PRODUCTION_TEXT = (
    "Подготовь один телевизор из этого прайса для Bitrix/Aspro по полной карточке товара. "
    "Заполни всё, что сможешь определить и подготовить: название, символьный код, артикул, "
    "EAN, бренд, закупочную и розничную цену, правильный раздел каталога, основное "
    "изображение, галерею, анонс, подробное описание и характеристики именно для "
    "телевизора. Используй существующий процесс Product Enrichment и существующие "
    "правила Panda для цены и Bitrix/Aspro. Ничего пока не записывай в Bitrix и не "
    "публикуй. Покажи мне итоговую подготовленную карточку и отдельно укажи только те "
    "поля, для которых действительно нет подтверждённого места записи в Bitrix/Aspro."
)
ANOTHER_ONE_TEXT = "Этот уже был. Дай другой."
FULL_CARD_FOR_NEW_PRODUCT_TEXT = (
    "Подготовь для этого товара полную карточку для Bitrix/Aspro: розничная цена, раздел "
    "каталога, основное изображение, галерея, анонс, подробное описание и характеристики. "
    "Ничего не записывай в Bitrix."
)

RESEARCH_URL_A = "https://www.lg.com/ru/tv/100mrgb96b6"
RESEARCH_URL_B = "https://www.lg.com/ru/tv/55mrgb86b6a"
IMAGE_URL_A = "https://www.lg.com/ru/photos/100mrgb96b6-hero.png"
IMAGE_URL_B = "https://www.lg.com/ru/photos/55mrgb86b6a-hero.png"


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append([PRODUCT_A_SKU, PRODUCT_A_NAME, CATEGORY, BRAND, PRODUCT_A_EAN, PRODUCT_A_PURCHASE_PRICE, PRODUCT_A_RETAIL_PRICE])
    ws.append([PRODUCT_B_SKU, PRODUCT_B_NAME, CATEGORY, BRAND, PRODUCT_B_EAN, PRODUCT_B_PURCHASE_PRICE, PRODUCT_B_RETAIL_PRICE])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (600, 600), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scrape_fetch_handler(request):
    import httpx

    html_by_url = {
        RESEARCH_URL_A: (
            f"<html><head><meta property=\"og:image\" content=\"{IMAGE_URL_A}\"></head><body>"
            f"<h1>LG {PRODUCT_A_SKU}</h1><table class=\"specs\">"
            "<tr><td>Диагональ экрана</td><td>100\"</td></tr>"
            "<tr><td>Частота обновления</td><td>120 Гц</td></tr>"
            "</table></body></html>"
        ),
        RESEARCH_URL_B: (
            f"<html><head><meta property=\"og:image\" content=\"{IMAGE_URL_B}\"></head><body>"
            f"<h1>LG {PRODUCT_B_SKU}</h1><table class=\"specs\">"
            "<tr><td>Диагональ экрана</td><td>55\"</td></tr>"
            "<tr><td>Частота обновления</td><td>120 Гц</td></tr>"
            "</table></body></html>"
        ),
    }
    html = html_by_url.get(str(request.url))
    if html is None:
        return httpx.Response(404)
    return httpx.Response(200, headers={"content-type": "text/html"}, text=html)


def _raw_tool_fields(*, name: str, sku: str, ean: str, purchase_price: str, retail_price: str) -> dict:
    """The EXACT raw shape ``managed_agent_poc.runtime_subprocess.
    _row_product_fields`` already produces for a resolved row -- used
    here only to script the (mocked) subprocess boundary, never to
    reimplement any business logic."""
    return {
        "name": name,
        "sku": sku,
        "ean": ean,
        "category": CATEGORY,
        "brand": BRAND,
        "purchase_price": purchase_price,
        "retail_price": retail_price,
    }


def _make_fake_run_turn(plan: list[dict]):
    """A deterministic double for ``ManagedAgentPOC.run_turn`` that never
    imports/launches the isolated OpenAI Agents SDK subprocess and never
    touches the network -- it reproduces exactly the tool-call SHAPES
    already proven live in ``tests/test_panda_managed_agent_integration.
    py`` (turn 1/2 -> ``select_product``, a later "show me the write
    plan" turn -> ``explain_bitrix_write_plan``), plus the SAME durable
    ``ConversationStateStore`` persistence the real isolated subprocess
    performs, so multi-turn eligibility/continuity works identically to
    production. This test is about the ``panda_bridge.py`` DELEGATION
    boundary, not about re-proving semantic tool selection (already
    proven live in that other file)."""

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

    return fake_run_turn


class ManagedAgentDelegatesToExistingProductPreparationTests(unittest.IsolatedAsyncioTestCase):
    """Production-faithful reproduction of the exact 3-turn conversation
    shape, with the Managed Agent path ENABLED end to end through the
    real ``WorkflowPandaConversationGateway.respond()`` entry point."""

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

    async def test_turn1_full_card_request_delegates_to_existing_enrichment_and_write_plan(self):
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-1", filename=FILENAME, content=_xlsx_bytes()
        )

        plan = [
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": [
                    {
                        "tool": "select_product",
                        "output": {
                            "status": "SELECTED",
                            "matched_by": "next_unspecified",
                            **_raw_tool_fields(
                                name=PRODUCT_A_NAME,
                                sku=PRODUCT_A_SKU,
                                ean=PRODUCT_A_EAN,
                                purchase_price=PRODUCT_A_PURCHASE_PRICE,
                                retail_price=PRODUCT_A_RETAIL_PRICE,
                            ),
                        },
                    }
                ],
                # What the raw, pre-fix model output looked like (a bare
                # row echo) -- proves the FINAL response text below is
                # NOT this raw text, i.e. delegation genuinely replaced it.
                "final_output": f"Товар {PRODUCT_A_NAME}, категория {CATEGORY}, закупочная цена {PRODUCT_A_PURCHASE_PRICE}.",
            }
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            result = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-1",
                    attachment_refs=(artifact_id,),
                )
            )

        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(result.metadata.get("managed_agent_tool"), "select_product")
        self.assertFalse(result.metadata.get("mutated"))
        self.assertEqual(
            result.metadata.get("delegated_to"),
            "product_enrichment_bridge.prepare_complete_card",
            "the turn must be delegated into the EXISTING deterministic preparation pipeline",
        )

        text = result.text
        # The response is NOT the raw, pre-fix row echo.
        self.assertNotEqual(text, f"Товар {PRODUCT_A_NAME}, категория {CATEGORY}, закупочная цена {PRODUCT_A_PURCHASE_PRICE}.")

        # PREPARED product state, not just the raw XLSX row: identity,
        # retail price (existing pricing rule: the row's own "розница"
        # column -- never invented), resolved Bitrix category, media, and
        # characteristics are all present -- this is the EXISTING
        # Product Enrichment / controlled Bitrix write-plan pipeline's
        # own rendering, verified independently below.
        self.assertIn(PRODUCT_A_SKU, text)
        self.assertIn(PRODUCT_A_EAN, text)
        self.assertIn(BRAND, text)
        self.assertIn(PRODUCT_A_RETAIL_PRICE, text)
        self.assertIn(str(TV_SECTION_ID), text)
        self.assertIn("Характеристики:", text)
        self.assertNotIn("Характеристики: 0", text)
        self.assertIn("Главное изображение подготовлено: да", text)
        write_preview = result.metadata.get("bitrix_write_preview") or {}
        self.assertEqual(write_preview.get("status"), "REQUIRES_APPROVAL")

        # PR #77 SIMPLE_PRODUCT contract: article/SKU has no verified
        # destination on the default (offer-free) simple product -- must
        # be reported as honestly UNMAPPED, never claimed as written.
        self.assertIn("НЕ будет записано (нет проверенного назначения в Bitrix):", text)
        self.assertIn("sku", text)

        # This is the SAME capability the legacy CALL_PRODUCT_ENRICHMENT
        # path already uses -- assert delegation, not a re-implementation:
        # calling the existing capability directly with the SAME inputs
        # must reproduce the SAME text (proves no duplicate/second
        # enrichment-and-write-plan logic was added in the bridge).
        from business_assistant.product_enrichment_bridge import prepare_complete_card
        from product_enrichment.cache import EnrichmentCache

        # A FRESH cache (never the shared ``self.panda._enrichment_cache``,
        # which the delegated call above already populated) so this
        # independent, direct call reproduces the SAME text byte-for-byte
        # -- proving the bridge's delegation is a genuine pass-through,
        # not a re-implementation with subtly different behavior.
        direct = await prepare_complete_card(
            tenant_id="tenant-a",
            product_fields={
                "title": PRODUCT_A_NAME,
                "sku": PRODUCT_A_SKU,
                "ean": PRODUCT_A_EAN,
                "category": CATEGORY,
                "brand": BRAND,
                "purchase_price": PRODUCT_A_PURCHASE_PRICE,
            },
            retail_price=PRODUCT_A_RETAIL_PRICE,
            bitrix_bridge=self.bridge,
            tool_gateway=self.panda._tool_gateway,  # noqa: SLF001
            media_fetcher=self.panda._media_fetcher,  # noqa: SLF001
            cache=EnrichmentCache(),
        )
        self.assertEqual(text, direct["text"])

        # ZERO real Bitrix mutation: only the read-only
        # ``catalog.section.list`` call happens; the recording transport
        # raises on any unexpected call (including ``catalog.product.add``).
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)

    async def test_multi_turn_another_one_then_full_card_prepares_product_b_not_stale_product_a(self):
        """Turn 1 selects product A. Turn 2 ("Этот уже был. Дай другой.")
        selects product B. Turn 3 asks for the full prepared Bitrix/Aspro
        card -- it must describe PRODUCT B, never stale PRODUCT A."""
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-2", filename=FILENAME, content=_xlsx_bytes()
        )

        plan = [
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": [
                    {
                        "tool": "select_product",
                        "output": {
                            "status": "SELECTED",
                            "matched_by": "next_unspecified",
                            **_raw_tool_fields(
                                name=PRODUCT_A_NAME,
                                sku=PRODUCT_A_SKU,
                                ean=PRODUCT_A_EAN,
                                purchase_price=PRODUCT_A_PURCHASE_PRICE,
                                retail_price=PRODUCT_A_RETAIL_PRICE,
                            ),
                        },
                    }
                ],
                "final_output": f"Товар {PRODUCT_A_NAME}.",
            },
            {
                "current_identifier": PRODUCT_B_SKU,
                "tool_calls": [
                    {
                        "tool": "select_product",
                        "output": {
                            "status": "SELECTED",
                            "matched_by": "next_unspecified",
                            **_raw_tool_fields(
                                name=PRODUCT_B_NAME,
                                sku=PRODUCT_B_SKU,
                                ean=PRODUCT_B_EAN,
                                purchase_price=PRODUCT_B_PURCHASE_PRICE,
                                retail_price=PRODUCT_B_RETAIL_PRICE,
                            ),
                        },
                    }
                ],
                "final_output": f"Товар {PRODUCT_B_NAME}.",
            },
            {
                "current_identifier": PRODUCT_B_SKU,
                "tool_calls": [
                    {
                        "tool": "explain_bitrix_write_plan",
                        "output": {
                            "status": "WRITE_PLAN",
                            "would_write": _raw_tool_fields(
                                name=PRODUCT_B_NAME,
                                sku=PRODUCT_B_SKU,
                                ean=PRODUCT_B_EAN,
                                purchase_price=PRODUCT_B_PURCHASE_PRICE,
                                retail_price=PRODUCT_B_RETAIL_PRICE,
                            ),
                            "note": "not written -- a separate, explicit write confirmation is required",
                        },
                    }
                ],
                "final_output": f"План записи для {PRODUCT_B_NAME}.",
            },
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            r1 = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-2",
                    attachment_refs=(artifact_id,),
                )
            )
            self.assertEqual(r1.metadata.get("action_decision"), "MANAGED_AGENT")
            self.assertIn(PRODUCT_A_SKU, r1.text)

            r2 = await self.panda.respond(
                ConversationRequest(
                    text=ANOTHER_ONE_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-2",
                )
            )
            self.assertEqual(r2.metadata.get("action_decision"), "MANAGED_AGENT")
            self.assertEqual(r2.metadata.get("managed_agent_tool"), "select_product")

            r3 = await self.panda.respond(
                ConversationRequest(
                    text=FULL_CARD_FOR_NEW_PRODUCT_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-3",
                    conversation_id="conv-2",
                )
            )

        self.assertEqual(r3.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r3.metadata.get("managed_agent_tool"), "explain_bitrix_write_plan")
        self.assertEqual(
            r3.metadata.get("delegated_to"),
            "product_enrichment_bridge.prepare_complete_card",
        )
        self.assertFalse(r3.metadata.get("mutated"))

        # Turn 3's prepared card is PRODUCT B, never stale PRODUCT A.
        self.assertIn(PRODUCT_B_SKU, r3.text)
        self.assertIn(PRODUCT_B_EAN, r3.text)
        self.assertIn(PRODUCT_B_RETAIL_PRICE, r3.text)
        self.assertNotIn(PRODUCT_A_SKU, r3.text)
        self.assertNotIn(PRODUCT_A_EAN, r3.text)

        write_preview = r3.metadata.get("bitrix_write_preview") or {}
        self.assertEqual(write_preview.get("status"), "REQUIRES_APPROVAL")
        self.assertIn("НЕ будет записано (нет проверенного назначения в Bitrix):", r3.text)
        self.assertIn("sku", r3.text)

        # ZERO Bitrix mutation across the whole 3-turn flow.
        self.assertNotIn("catalog.product.add", [m for m, _ in self.transport.calls])
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)

    async def test_no_product_resolved_falls_back_to_raw_model_output_unchanged(self):
        """A pure whole-spreadsheet-analysis turn resolves no specific
        product -- the response must be the model's own output, completely
        unaffected by the new delegation code path (never a crash, never
        an unrelated enrichment call)."""
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-3", filename=FILENAME, content=_xlsx_bytes()
        )
        plan = [
            {
                "tool_calls": [{"tool": "analyze_spreadsheet", "output": {"row_count": 2, "column_count": 7}}],
                "final_output": "В прайсе 2 товара.",
            }
        ]
        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            result = await self.panda.respond(
                ConversationRequest(
                    text="Сколько товаров в этом прайсе?",
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-3",
                    attachment_refs=(artifact_id,),
                )
            )
        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(result.metadata.get("managed_agent_tool"), "analyze_spreadsheet")
        self.assertNotIn("delegated_to", result.metadata)
        self.assertEqual(result.text, "В прайсе 2 товара.")


class PandaBridgeDelegationUnitTests(unittest.IsolatedAsyncioTestCase):
    """Fast, direct unit checks of the new helper functions in
    ``managed_agent_poc/panda_bridge.py`` -- no subprocess, no network."""

    def test_selected_product_raw_fields_prefers_last_resolved_call(self):
        from managed_agent_poc.panda_bridge import _selected_product_raw_fields

        tool_calls = [
            {"tool": "select_product", "output": {"status": "SELECTED", "matched_by": "sku", "name": "A", "sku": "SKU-A"}},
            {"tool": "explain_bitrix_write_plan", "output": {"status": "WRITE_PLAN", "would_write": {"name": "A", "sku": "SKU-A"}}},
        ]
        resolved = _selected_product_raw_fields(tool_calls)
        self.assertEqual(resolved, {"name": "A", "sku": "SKU-A"})

    def test_selected_product_raw_fields_none_when_no_product_resolved(self):
        from managed_agent_poc.panda_bridge import _selected_product_raw_fields

        self.assertIsNone(_selected_product_raw_fields([{"tool": "analyze_spreadsheet", "output": {"row_count": 3}}]))
        self.assertIsNone(_selected_product_raw_fields([{"tool": "select_product", "output": {"status": "NOT_FOUND"}}]))
        self.assertIsNone(_selected_product_raw_fields([]))

    def test_canonical_fields_translates_names_without_inventing_values(self):
        from managed_agent_poc.panda_bridge import _canonical_fields_and_retail_price

        fields, retail_price = _canonical_fields_and_retail_price(
            {"name": "LG TV", "sku": "S1", "ean": "111", "category": "TV", "brand": "LG", "purchase_price": "100", "retail_price": "150"}
        )
        self.assertEqual(
            fields,
            {"title": "LG TV", "sku": "S1", "ean": "111", "category": "TV", "brand": "LG", "purchase_price": "100"},
        )
        self.assertEqual(retail_price, "150")

    def test_canonical_fields_never_derives_a_missing_retail_price(self):
        from managed_agent_poc.panda_bridge import _canonical_fields_and_retail_price

        _fields, retail_price = _canonical_fields_and_retail_price({"name": "LG TV", "sku": "S1", "purchase_price": "100"})
        self.assertEqual(retail_price, "")

    async def test_delegate_returns_none_without_title_or_sku(self):
        from managed_agent_poc.panda_bridge import _delegate_to_existing_product_preparation

        result = await _delegate_to_existing_product_preparation({"category": "TV"}, tenant_id="tenant-a")
        self.assertIsNone(result)

    async def test_delegate_calls_existing_prepare_complete_card_exactly_once(self):
        from managed_agent_poc.panda_bridge import _delegate_to_existing_product_preparation

        with mock.patch(
            "business_assistant.product_enrichment_bridge.prepare_complete_card",
            new=mock.AsyncMock(return_value={"text": "OK", "write_preview": {}}),
        ) as mocked:
            result = await _delegate_to_existing_product_preparation(
                {"name": "LG TV", "sku": "S1", "ean": "111", "category": "TV", "brand": "LG", "purchase_price": "100", "retail_price": "150"},
                tenant_id="tenant-a",
            )
        self.assertEqual(result, {"text": "OK", "write_preview": {}})
        mocked.assert_awaited_once()
        _args, kwargs = mocked.call_args
        self.assertEqual(kwargs["tenant_id"], "tenant-a")
        self.assertEqual(kwargs["retail_price"], "150")
        self.assertEqual(
            kwargs["product_fields"],
            {"title": "LG TV", "sku": "S1", "ean": "111", "category": "TV", "brand": "LG", "purchase_price": "100"},
        )

    async def test_delegate_fails_open_when_existing_pipeline_raises(self):
        from managed_agent_poc.panda_bridge import _delegate_to_existing_product_preparation

        with mock.patch(
            "business_assistant.product_enrichment_bridge.prepare_complete_card",
            new=mock.AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await _delegate_to_existing_product_preparation(
                {"name": "LG TV", "sku": "S1"}, tenant_id="tenant-a"
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
