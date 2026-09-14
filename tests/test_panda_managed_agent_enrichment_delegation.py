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

==================================================================
PRODUCTION DEFECT CLOSURE #2 (real production, AFTER the above fix
shipped as PR #79): the returned card was STILL degraded/raw-row-like.
==================================================================

Proven root cause: the fix above (delegation) is functionally correct --
it was verified, in this same defect closure, against the REAL, live
``ManagedAgentPOC.run_turn()`` output (a genuine OpenAI Agents SDK call,
not a hand-crafted double) for multiple representative price-list
shapes, including the exact shape that reproduces the reported
production symptom (a price list with NO distinct retail-price column):
delegation into ``prepare_complete_card`` fired correctly and replaced
the raw model text every single time this was exercised directly. The
remaining, still-open gap is that ``_delegate_to_existing_product_
preparation`` had exactly ONE way to fail closed and completely silent:
any exception raised by the existing pipeline (real research/media-fetch
network I/O, a real Bitrix connection issue, or any other real-deployment-
specific condition this sandbox's fixtures/mocks do not reproduce) was
caught and converted into a bare ``None`` -- and the caller's ONLY
fallback for that ``None`` was to re-present the turn's ORIGINAL raw
Managed Agent tool/model text, with NO signal anywhere (to the user or in
metadata) that the "completed card" being shown was actually never
produced by the deterministic pipeline at all. This is hypothesis (F)
from the task: "the delegation throws and #79's fail-open silently
returns the old raw Managed Agent output" -- and it is the exact
mechanism that explains the reported symptom (a model-synthesized
"розничная цена не рассчитана (нужен расчёт по правилам Panda)" /
"Поля без подтверждённого места записи в Bitrix/Aspro: Нет таких полей"
text, which is recognizably the RAW model's own free-form summary of an
unresolved tool projection, not ``format_combined_preview_text``'s fixed,
deterministic rendering).

Fix (this change, ``managed_agent_poc/panda_bridge.py``):
1. Safe, non-secret, greppable diagnostic events (``EVENT_MANAGED_
   PRODUCT_SELECTED`` / ``EVENT_PRODUCT_PREPARATION_DELEGATION_STARTED``
   / ``_SUCCEEDED`` / ``_FAILED`` / ``_REQUIRED_BUT_NOT_REACHED``) at
   every decision point, so a future occurrence in production is finally
   directly observable instead of indistinguishable from a genuine
   success.
2. ``_delegate_to_existing_product_preparation`` now returns
   ``(result, reason)`` instead of a bare ``result | None`` -- ``reason``
   is one of a small, static set of non-secret codes
   (``"missing_title_or_sku"``, ``"timeout"``,
   ``f"exception:{type(exc).__name__}"``) a caller/operator can act on.
3. THE central contract fix (Step 2 of the task): when a product WAS
   resolved this turn (i.e. complete preparation was semantically
   required) but delegation fails for ANY reason, ``maybe_respond_via_
   managed_agent`` no longer silently falls back to the raw Managed
   Agent tool/model text. It now returns an EXPLICIT, honest
   ``_controlled_preparation_failure_text`` -- only the already-confirmed
   raw identity fields, a clear "preparation not completed" statement,
   and the safe failure reason -- plus ``metadata["preparation_status"]
   == "FAILED"`` (vs. ``"PREPARED"`` on success). This is a semantic/
   tool-state distinction (a product WAS resolved this turn), never
   phrase matching, and never touches the separate, correct behavior for
   a pure spreadsheet-analysis turn (no product resolved -> the model's
   own output is still returned unchanged, see
   ``test_no_product_resolved_falls_back_to_raw_model_output_unchanged``).
4. A bounded delegation timeout (``DEFAULT_DELEGATION_TIMEOUT_S``) so a
   slow real dependency degrades to the same explicit failure contract
   instead of hanging the turn indefinitely.

New tests below prove: the honest failure contract fires end to end
through the real ``WorkflowPandaConversationGateway.respond()`` entry
point when delegation raises (``ManagedAgentDelegationFailureContractTests``);
and the delegation success path is verified against a LITERAL, real-
subprocess-captured ``tool_calls``/``final_output`` shape, not a
hand-crafted guess (``ManagedAgentRealSubprocessContractTests`` --
this is exactly the "test double differed from real ManagedAgentPOC
output" gap the task asked to close). A further LIVE test (skipped
cleanly without a real ``OPENAI_API_KEY``/installed SDK, same convention
as ``tests/test_panda_managed_agent_integration.py``) exercises a REAL,
non-mocked ``ManagedAgentPOC.run_turn()`` call end to end through the
full delegation boundary (``LiveRealSubprocessDelegationTests``).
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
from managed_agent_poc import isolated_env
from managed_agent_poc.adapter import ManagedAgentPOC, ManagedAgentTurnResult
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR, _durable_paths
from managed_agent_poc.state_store import ConversationStateStore, PersistedState
from product_enrichment.media_fetch import FakeImageFetcher
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tools.search.fake_provider import FakeSearchProvider, fake_result

_SDK_AVAILABLE = isolated_env.is_installed()
_HAS_KEY = bool(os.environ.get("OPENAI_API_KEY"))
_LIVE_SKIP_REASON = "isolated OpenAI Agents SDK not installed or OPENAI_API_KEY not set in this environment"

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
# The real production follow-up request (production defect closure #4):
# "показать финальный план записи уже подготовленной карточки, без
# записи", explicitly asking for the deterministic controlled Bitrix
# write-plan (SIMPLE_PRODUCT, exact section ID, purchase/retail price,
# verified destinations, #77 unmapped fields) -- never a write/update/
# publication.
WRITE_PLAN_TEXT = (
    "Покажи финальный план записи уже подготовленной карточки товара в "
    "Bitrix/Aspro, без записи: модель товара (обычный товар или товар с "
    "торговым предложением), точный раздел каталога и его ID, закупочную "
    "цену, розничную цену, рассчитанную по существующим правилам Panda, "
    "и для каждого подготовленного поля укажи, подтверждено ли для него "
    "место записи в Bitrix/Aspro. Ничего не записывай, не обновляй и не "
    "публикуй."
)
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

    async def test_full_card_request_not_diverted_to_write_plan_when_write_plan_tool_also_called_first(self):
        """PRODUCTION DEFECT CLOSURE #5 (competing-path defect after PR
        #82): PREPARE_PRODUCT (``select_product``) and SHOW_WRITE_PLAN
        (``explain_bitrix_write_plan``) are two DISTINCT semantic actions
        (see PR #82). Real ``ManagedAgentPOC.run_turn()`` output can
        legitimately contain MORE THAN ONE tool call in a single turn --
        the Agents SDK's own agentic loop lets the model call a tool,
        see its result, and call ANOTHER tool before producing its final
        answer (already proven live for other shapes in
        ``ManagedAgentRealSubprocessContractTests``). For this turn's
        full-card ``PRODUCTION_TEXT`` request, the model can reasonably
        attempt ``explain_bitrix_write_plan`` FIRST (its own docstring
        mentions confirming "the write plan / fields / category that
        would be sent to Bitrix/Aspro", which overlaps with this
        request's own "укажи только те поля, для которых действительно
        нет подтверждённого места записи в Bitrix/Aspro" wording) --
        that call fails with ``NO_PRODUCT_SELECTED`` (nothing has been
        selected yet in this brand-new conversation) -- and only THEN
        calls ``select_product``, which succeeds with ``SELECTED``.

        Before this fix, ``maybe_respond_via_managed_agent`` computed its
        PREPARE_PRODUCT-vs-SHOW_WRITE_PLAN routing decision from
        ``turn_result.tool_calls[0]["tool"]`` -- the turn's FIRST tool
        call -- while ``_selected_product_raw_fields`` (which decides
        WHAT to delegate) scans for the LAST tool call that actually
        resolved a product. For this exact shape those two disagree:
        ``tool_calls[0]`` is ``explain_bitrix_write_plan`` (the failed
        attempt) while the actual resolution came from ``select_product``
        (the second call) -- so a genuine PREPARE_PRODUCT/full-card
        action was WRONGLY routed into ``_delegate_to_existing_write_
        plan``/``format_write_plan_text`` (the SHOW_WRITE_PLAN renderer)
        instead of ``_delegate_to_existing_product_preparation``/
        ``prepare_complete_card`` (the full-card renderer) -- the exact
        "PREPARE_PRODUCT and SHOW_WRITE_PLAN collapse into the same
        response" defect PR #82 already closed in the OTHER direction.

        This must FAIL on current `main` before the fix (routes to
        ``format_write_plan_text``) and PASS after it (routes to
        ``prepare_complete_card``, the SAME deterministic full-card
        result as the single-tool-call turn 1 test above)."""
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-competing", filename=FILENAME, content=_xlsx_bytes()
        )

        plan = [
            {
                "current_identifier": PRODUCT_A_SKU,
                "tool_calls": [
                    {
                        "tool": "explain_bitrix_write_plan",
                        "output": {
                            "status": "NO_PRODUCT_SELECTED",
                            "reason": "no product has been selected in this conversation yet",
                        },
                    },
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
                    },
                ],
                "final_output": f"Подготовлена карточка {PRODUCT_A_NAME}.",
            }
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            result = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-competing",
                    attachment_refs=(artifact_id,),
                )
            )

        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertFalse(result.metadata.get("mutated"))

        # THE authoritative assertion: a PREPARE_PRODUCT/full-card action
        # must delegate into the full-card renderer -- never the
        # write-plan renderer -- regardless of which OTHER tool the model
        # also attempted earlier in the same turn.
        self.assertEqual(
            result.metadata.get("delegated_to"),
            "product_enrichment_bridge.prepare_complete_card",
            "a resolved select_product/PREPARE_PRODUCT turn must never be "
            "diverted into the SHOW_WRITE_PLAN renderer just because an "
            "earlier, failed tool call in the same turn was "
            "explain_bitrix_write_plan",
        )

        text = result.text
        self.assertNotIn("ЧТО БУДЕТ ЗАПИСАНО В BITRIX/ASPRO", text)

        # The full, enriched deterministic PREPARED result -- identity,
        # retail price, resolved category, media, characteristics, and
        # honest #77 unmapped reporting -- exactly like the single-call
        # turn 1 test above, never a shallower/different rendering.
        self.assertIn(PRODUCT_A_SKU, text)
        self.assertIn(PRODUCT_A_EAN, text)
        self.assertIn(BRAND, text)
        self.assertIn(PRODUCT_A_RETAIL_PRICE, text)
        self.assertIn(str(TV_SECTION_ID), text)
        self.assertIn("Характеристики:", text)
        self.assertNotIn("Характеристики: 0", text)
        self.assertIn("Главное изображение подготовлено: да", text)
        self.assertIn("НЕ будет записано (нет проверенного назначения в Bitrix):", text)
        self.assertIn("sku", text)

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
        # Production defect closure #4: SHOW_WRITE_PLAN (this tool) is a
        # DISTINCT semantic action from PREPARE_PRODUCT (``select_product``,
        # asserted in the OTHER test in this class) and must delegate into
        # the existing deterministic WRITE-PLAN renderer, never
        # ``prepare_complete_card``'s own full-card preview text.
        self.assertEqual(
            r3.metadata.get("delegated_to"),
            "product_enrichment_bridge.format_write_plan_text",
        )
        self.assertFalse(r3.metadata.get("mutated"))

        # Turn 3's write plan is for PRODUCT B, never stale PRODUCT A --
        # reusing turn 2's already-selected/already-enriched product,
        # never re-selecting or losing identity.
        self.assertIn(PRODUCT_B_SKU, r3.text)
        self.assertIn(PRODUCT_B_EAN, r3.text)
        self.assertIn(PRODUCT_B_RETAIL_PRICE, r3.text)
        self.assertNotIn(PRODUCT_A_SKU, r3.text)
        self.assertNotIn(PRODUCT_A_EAN, r3.text)

        # This is a genuine WRITE PLAN (``format_write_plan_text``), not
        # the generic full-card preview: SIMPLE_PRODUCT contract text,
        # the resolved section ID, and the field-by-field write/no-write
        # breakdown are all present.
        self.assertIn("ЧТО БУДЕТ ЗАПИСАНО В BITRIX/ASPRO", r3.text)
        self.assertIn("SIMPLE_PRODUCT", r3.metadata.get("bitrix_write_preview", {}).get("product_model", {}).get("model", ""))
        self.assertIn(f"ID {TV_SECTION_ID}", r3.text)

        write_preview = r3.metadata.get("bitrix_write_preview") or {}
        self.assertEqual(write_preview.get("status"), "REQUIRES_APPROVAL")
        self.assertIn("НЕ будет записано: sku", r3.text)
        self.assertIn("НЕ будет записано: ean", r3.text)

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

        result, reason = await _delegate_to_existing_product_preparation({"category": "TV"}, tenant_id="tenant-a")
        self.assertIsNone(result)
        self.assertEqual(reason, "missing_title_or_sku")

    async def test_delegate_calls_existing_prepare_complete_card_exactly_once(self):
        from managed_agent_poc.panda_bridge import _delegate_to_existing_product_preparation

        with mock.patch(
            "business_assistant.product_enrichment_bridge.prepare_complete_card",
            new=mock.AsyncMock(return_value={"text": "OK", "write_preview": {}}),
        ) as mocked:
            result, reason = await _delegate_to_existing_product_preparation(
                {"name": "LG TV", "sku": "S1", "ean": "111", "category": "TV", "brand": "LG", "purchase_price": "100", "retail_price": "150"},
                tenant_id="tenant-a",
            )
        self.assertEqual(result, {"text": "OK", "write_preview": {}})
        self.assertEqual(reason, "")
        mocked.assert_awaited_once()
        _args, kwargs = mocked.call_args
        self.assertEqual(kwargs["tenant_id"], "tenant-a")
        self.assertEqual(kwargs["retail_price"], "150")
        self.assertEqual(
            kwargs["product_fields"],
            {"title": "LG TV", "sku": "S1", "ean": "111", "category": "TV", "brand": "LG", "purchase_price": "100"},
        )

    async def test_delegate_fails_open_when_existing_pipeline_raises(self):
        """Production defect closure #2's own hypothesis (F): if the
        EXISTING pipeline itself raises for any reason, delegation must
        report an EXPLICIT, non-secret reason code (never silently return
        a bare ``None`` a caller could mistake for a different failure
        mode)."""
        from managed_agent_poc.panda_bridge import _delegate_to_existing_product_preparation

        with mock.patch(
            "business_assistant.product_enrichment_bridge.prepare_complete_card",
            new=mock.AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result, reason = await _delegate_to_existing_product_preparation(
                {"name": "LG TV", "sku": "S1"}, tenant_id="tenant-a"
            )
        self.assertIsNone(result)
        self.assertEqual(reason, "exception:RuntimeError")

    async def test_delegate_reports_timeout_instead_of_hanging(self):
        """A slow real dependency (real research/media-fetch network I/O
        in a real deployment) must degrade to an explicit ``"timeout"``
        reason within the bounded ``timeout_s``, never hang the turn."""
        import asyncio

        from managed_agent_poc.panda_bridge import _delegate_to_existing_product_preparation

        async def _hangs(**_kwargs):
            await asyncio.sleep(10.0)
            return {"text": "too late"}

        with mock.patch(
            "business_assistant.product_enrichment_bridge.prepare_complete_card",
            new=_hangs,
        ):
            result, reason = await _delegate_to_existing_product_preparation(
                {"name": "LG TV", "sku": "S1"}, tenant_id="tenant-a", timeout_s=0.05
            )
        self.assertIsNone(result)
        self.assertEqual(reason, "timeout")

    def test_controlled_preparation_failure_text_never_claims_completed_card(self):
        """Step 2 contract fix: the honest failure message must surface
        ONLY the raw fields already confirmed by the price list, and must
        explicitly say retail price/media/characteristics/Bitrix mapping
        were NOT prepared -- never silently presenting them as ready or
        omitting the failure entirely (the exact defect real production
        exposed)."""
        from managed_agent_poc.panda_bridge import _controlled_preparation_failure_text

        text = _controlled_preparation_failure_text(
            {
                "name": "Телевизор LG 100MRGB96B6.ARUG",
                "sku": "100MRGB96B6.ARUG",
                "ean": "8806096796849",
                "brand": "LG",
                "category": "Телевизоры",
                "purchase_price": "717790.30",
            },
            reason="exception:RuntimeError",
        )
        self.assertIn("НЕ ЗАВЕРШЕНА", text)
        self.assertIn("100MRGB96B6.ARUG", text)
        self.assertIn("8806096796849", text)
        self.assertIn("exception:RuntimeError", text)
        self.assertIn("НЕ подготовлены", text)
        self.assertIn("ничего не записано", text.lower())
        # Never a false claim that every field has a confirmed Bitrix
        # destination, and never a fabricated retail price/section.
        self.assertNotIn("Нет таких полей", text)


class ManagedAgentDelegationFailureContractTests(unittest.IsolatedAsyncioTestCase):
    """Production defect closure #2 -- STEP 2: when a product IS resolved
    this turn but the existing deterministic preparation pipeline itself
    fails, the turn must return an EXPLICIT controlled preparation
    failure, never the raw, un-enriched Managed Agent tool/model
    projection silently presented as if it were the completed card (this
    is the exact defect the real production run exposed AFTER #79 already
    shipped delegation -- #79's own unit test never exercised the
    "delegation raises" branch end to end through ``maybe_respond_via_
    managed_agent``, only through the lower-level helper in isolation)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        self.panda, self.artifact_service = _panda()

    async def asyncTearDown(self):
        if self._old_data_dir is None:
            os.environ.pop("PANDA_DATA_DIR", None)
        else:
            os.environ["PANDA_DATA_DIR"] = self._old_data_dir
        if self._old_flag is None:
            os.environ.pop(ENABLED_ENV_VAR, None)
        else:
            os.environ[ENABLED_ENV_VAR] = self._old_flag
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_delegation_exception_returns_explicit_failure_not_raw_echo(self):
        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-fail-1", filename=FILENAME, content=_xlsx_bytes()
        )
        raw_model_text = f"Товар {PRODUCT_A_NAME}, категория {CATEGORY}, закупочная цена {PRODUCT_A_PURCHASE_PRICE}."
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
                "final_output": raw_model_text,
            }
        ]
        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)), mock.patch(
            "business_assistant.product_enrichment_bridge.prepare_complete_card",
            new=mock.AsyncMock(side_effect=RuntimeError("simulated real-production dependency failure")),
        ):
            result = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-fail-1",
                    attachment_refs=(artifact_id,),
                )
            )

        # The exact defect real production exposed: the raw model/tool
        # text must NEVER be silently presented as the completed card.
        self.assertNotEqual(result.text, raw_model_text)
        self.assertIn("НЕ ЗАВЕРШЕНА", result.text)
        self.assertIn(PRODUCT_A_SKU, result.text)
        self.assertIn(PRODUCT_A_EAN, result.text)
        self.assertNotIn("Нет таких полей", result.text)

        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(result.metadata.get("preparation_status"), "FAILED")
        self.assertEqual(result.metadata.get("preparation_failure_reason"), "exception:RuntimeError")
        self.assertNotIn("delegated_to", result.metadata)
        self.assertFalse(result.metadata.get("mutated"))


class ManagedAgentRealSubprocessContractTests(unittest.IsolatedAsyncioTestCase):
    """Production defect closure #2 -- CRITICAL ACCEPTANCE ASSERTION:
    exercises the REAL, serialized ``ManagedAgentPOC.run_turn()`` output
    shape captured from an ACTUAL run of the isolated OpenAI Agents SDK
    subprocess against a production-shaped LG_TV-style price list (NOT a
    hand-crafted test double that might silently drift from what the real
    runtime actually serializes -- exactly the gap #79's own test left
    open: it only ever exercised ``_make_fake_run_turn``'s hand-crafted
    tool-call shape, so a subtle mismatch between that shape and the real
    subprocess's own serialization would never have been caught).

    The literal ``tool_calls``/``final_output`` fixtures below were
    captured verbatim from real ``ManagedAgentPOC.run_turn()`` calls (see
    the PR description for the exact reproduction commands) against a
    representative production-shaped price-list row that has NO distinct
    retail-price column (reproducing the reported "розничная цена не
    рассчитана" / "не найдено в прайсе" production symptom). No live
    OpenAI call is required to run this test -- only ``ManagedAgentPOC.
    run_turn`` is replaced, exactly as much of the boundary as #79's own
    test replaced, but with the REAL, not reconstructed, serialized
    shape."""

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
            search_provider=FakeSearchProvider({}),
            media_fetcher=FakeImageFetcher({}),
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

    async def test_real_subprocess_shape_without_retail_price_column_still_delegates(self):
        """Captured verbatim from a REAL ``ManagedAgentPOC.run_turn()``
        call against a price list with sku/product_name/category/brand/
        ean/purchase_price columns but NO retail-price column (the exact
        shape that reproduced the reported production symptom locally:
        ``final_output`` reads "Розничная цена: не определена в прайсе",
        "Основное изображение: не найдено в прайсе" -- proving this was
        the RAW model synthesis, not the deterministic pipeline's own
        rendering)."""
        real_tool_calls = [
            {
                "tool": "select_product",
                "output": {
                    "status": "SELECTED",
                    "matched_by": "next_unspecified",
                    "sku": PRODUCT_A_SKU,
                    "name": PRODUCT_A_NAME,
                    "category": CATEGORY,
                    "brand": BRAND,
                    "ean": PRODUCT_A_EAN,
                    "purchase_price": PRODUCT_A_PURCHASE_PRICE,
                },
            }
        ]
        real_final_output = (
            "Подготовлена карточка товара на основе прайса:\n\n"
            f"- **Название:** {PRODUCT_A_NAME}\n"
            f"- **Артикул:** `{PRODUCT_A_SKU}`\n"
            f"- **EAN:** `{PRODUCT_A_EAN}`\n"
            f"- **Бренд:** {BRAND}\n"
            f"- **Закупочная цена:** {PRODUCT_A_PURCHASE_PRICE}\n"
            "- **Розничная цена:** не определена в прайсе\n"
            f"- **Раздел каталога:** {CATEGORY}\n"
            "- **Основное изображение:** не найдено в прайсе\n"
            "- **Галерея:** не найдена\n\n"
            "**Поля без подтверждённого места записи в Bitrix/Aspro:** "
            "розничная цена, изображения и галерея, характеристики.\n\n"
            "В Bitrix/Aspro ничего не записывалось и не публиковалось."
        )
        plan = [{"current_identifier": PRODUCT_A_SKU, "tool_calls": real_tool_calls, "final_output": real_final_output}]

        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-real-1", filename=FILENAME, content=_xlsx_bytes()
        )
        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            result = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-real-1",
                    attachment_refs=(artifact_id,),
                )
            )

        # This is the EXACT real production regression: the raw model
        # synthesis must NEVER be the final response once a product was
        # resolved -- delegation into the existing deterministic pipeline
        # must have replaced it.
        self.assertNotEqual(result.text, real_final_output)
        self.assertEqual(result.metadata.get("delegated_to"), "product_enrichment_bridge.prepare_complete_card")
        self.assertEqual(result.metadata.get("preparation_status"), "PREPARED")
        self.assertIn(PRODUCT_A_SKU, result.text)
        self.assertIn(PRODUCT_A_EAN, result.text)
        # No fake/derived retail price: the real pipeline honestly reports
        # the SAME missing-retail-price state the raw row itself had,
        # never inventing one, and never asking the user for a pricing
        # coefficient (Step 4's own requirement).
        self.assertNotIn("коэффициент", result.text.lower())
        self.assertFalse(result.metadata.get("mutated"))
        self.assertNotIn("catalog.product.add", [m for m, _ in self.transport.calls])


@unittest.skipUnless(_SDK_AVAILABLE, _LIVE_SKIP_REASON)
class ManagedAgentArticleRoleSkuIdentityLossTests(unittest.IsolatedAsyncioTestCase):
    """Production defect closure #3 -- REAL Railway evidence after #80:

        MANAGED_PRODUCT_SELECTED tool=select_product
            has_sku=False has_name=True has_retail_price=False
        PRODUCT_PREPARATION_DELEGATION_FAILED reason=missing_title_or_sku

    even though the price-list row DOES have an identifier column with
    value ``100MRGB96B6.ARUG`` -- it is simply headed "Артикул", which
    ``data_intel.mapping`` classifies as the DISTINCT semantic role
    ``ROLE_ARTICLE`` (see ``data_intel/mapping.py``: ``"артикул":
    ROLE_ARTICLE``), not ``ROLE_SKU``. This is EXACTLY the same fallback
    the existing, already-proven ``data_intel.service._row_lookup_
    result`` has always used for its own ``product_fields["sku"]``:
    ``_role_value(row, table, ROLE_SKU) or _role_value(row, table,
    ROLE_ARTICLE)``. ``managed_agent_poc.runtime_subprocess.
    _row_product_fields`` (the tool-output row-projection ``select_
    product`` itself returns) had NO mapping for ``ROLE_ARTICLE`` at
    all, so the value was silently dropped before it ever reached
    ``select_product``'s returned dict -- proven directly below.

    CRITICAL ACCEPTANCE ASSERTION: this test does NOT mock away the
    extraction/canonicalization boundary being fixed. It drives the
    REAL, unmocked ``ManagedAgentPOC.run_turn()`` (only the MODEL's tool-
    selection DECISION is scripted via the SDK's own no-API-key
    ``ScriptedModel`` testing utility -- exactly the same convention
    ``tests/test_managed_agent_poc.py``'s own ``ManagedAgentOrchestration
    Tests`` already uses -- the tool's OWN business logic,
    ``select_product``/``_row_product_fields``, executes for real
    against a real ingested XLSX row), then feeds that REAL tool-call
    output through the REAL, unmocked ``panda_bridge._selected_product_
    raw_fields``/``_canonical_fields_and_retail_price``/``_delegate_to_
    existing_product_preparation`` -- proving the fix all the way
    through to ``prepare_complete_card`` being reached, not merely that
    a hand-crafted dict happens to contain a "sku" key."""

    ARTICLE_HEADER_SKU = "100MRGB96B6.ARUG"
    ARTICLE_HEADER_NAME = "Телевизор LG 100MRGB96B6.ARUG"
    ARTICLE_HEADER_EAN = "8806096796849"
    ARTICLE_HEADER_PURCHASE_PRICE = "717790.30"

    def setUp(self):
        # NOTE: ``poc.run_turn`` is driven DIRECTLY (not through
        # ``panda_bridge.maybe_respond_via_managed_agent``) so this uses
        # the isolated-subprocess-adapter's OWN flag
        # (``managed_agent_poc.flags.FLAG_ENV_VAR`` ==
        # "PANDA_MANAGED_AGENT_POC_ENABLED") -- the SAME one ``tests/
        # test_managed_agent_poc.py``'s ``_EnabledFlagMixin`` uses --
        # never ``panda_bridge.ENABLED_ENV_VAR``
        # ("PANDA_MANAGED_AGENT_ENABLED"), which only gates the separate
        # Panda-conversation-gateway integration boundary.
        from managed_agent_poc.flags import FLAG_ENV_VAR

        os.environ[FLAG_ENV_VAR] = "true"
        self.addCleanup(lambda: os.environ.pop(FLAG_ENV_VAR, None))
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

        # The EXACT real production column header ("Артикул", not "sku")
        # for the control row from the task -- 100MRGB96B6.ARUG / EAN
        # 8806096796849 / purchase price 717790.30/717790.20/717790.3
        # (all reported in different production log lines for the same
        # control row; this fixture uses the XLSX/task-stated value).
        wb = Workbook()
        ws = wb.active
        ws.append(["Артикул", "product_name", "category", "brand", "ean", "purchase_price"])
        ws.append(
            [
                self.ARTICLE_HEADER_SKU,
                self.ARTICLE_HEADER_NAME,
                CATEGORY,
                BRAND,
                self.ARTICLE_HEADER_EAN,
                self.ARTICLE_HEADER_PURCHASE_PRICE,
            ]
        )
        buf = io.BytesIO()
        wb.save(buf)
        self.xlsx_path = os.path.join(self.tmp, FILENAME)
        with open(self.xlsx_path, "wb") as fh:
            fh.write(buf.getvalue())

        self.poc = ManagedAgentPOC(
            dataset_store_path=os.path.join(self.tmp, "dataset.sqlite3"),
            session_db_path=os.path.join(self.tmp, "session.sqlite3"),
            state_store_path=os.path.join(self.tmp, "state.sqlite3"),
        )

    async def test_article_role_sku_survives_select_product_and_reaches_delegation_guard(self):
        from managed_agent_poc.panda_bridge import (
            _canonical_fields_and_retail_price,
            _delegate_to_existing_product_preparation,
            _selected_product_raw_fields,
        )

        # Step 1 -- the REAL ``select_product`` tool output (only the
        # model's DECISION to call this tool is scripted; the tool's own
        # ``_row_product_fields`` runs unmodified against the real
        # ingested "Артикул" row).
        turn_result = self.poc.run_turn(
            text=PRODUCTION_TEXT,
            tenant_id="tenant-article-role",
            conversation_id="conv-article-role-1",
            artifact_bytes_path=self.xlsx_path,
            artifact_filename=FILENAME,
            test_scripted_plan=[
                {"call_tool": "select_product", "arguments": {"identifier": None}},
                {"final_output": "Product prepared, not written to Bitrix."},
            ],
        )
        self.assertEqual(turn_result.status, "COMPLETED")
        select_output = turn_result.tool_calls[0]["output"]
        self.assertEqual(select_output.get("status"), "SELECTED")
        # Proves the REAL fix inside ``select_product``/``_row_product_
        # fields`` itself -- before this fix, ``select_output`` had a
        # "name" key but NO "sku" key at all (the exact real production
        # shape logged as ``has_sku=False has_name=True``).
        self.assertTrue(select_output.get("name"))
        self.assertEqual(select_output.get("sku"), self.ARTICLE_HEADER_SKU)

        # Step 2 -- the REAL extraction/canonicalization boundary.
        raw_fields = _selected_product_raw_fields(turn_result.tool_calls)
        self.assertIsNotNone(raw_fields)
        self.assertTrue(raw_fields.get("name"))
        self.assertEqual(raw_fields.get("sku"), self.ARTICLE_HEADER_SKU)

        product_fields, _retail_price = _canonical_fields_and_retail_price(raw_fields)
        self.assertEqual(product_fields["sku"], self.ARTICLE_HEADER_SKU)
        self.assertTrue(product_fields["title"])

        # Step 3 -- proves the call now passes beyond the existing
        # ``missing_title_or_sku`` guard and actually reaches the
        # EXISTING ``prepare_complete_card`` (observed here via a patch
        # that records invocation + echoes back the received
        # ``product_fields`` verbatim, without reimplementing or
        # short-circuiting any of its own business logic).
        received: dict = {}

        async def _fake_prepare_complete_card(*, tenant_id, product_fields, **kwargs):
            received["tenant_id"] = tenant_id
            received["product_fields"] = product_fields
            return {"text": "PREPARED", "write_preview": {}}

        with mock.patch(
            "business_assistant.product_enrichment_bridge.prepare_complete_card",
            new=_fake_prepare_complete_card,
        ):
            delegated, reason = await _delegate_to_existing_product_preparation(
                raw_fields,
                tenant_id="tenant-article-role",
                timeout_s=30.0,
            )

        self.assertEqual(reason, "")
        self.assertIsNotNone(delegated)
        self.assertEqual(delegated.get("text"), "PREPARED")
        self.assertNotEqual(reason, "missing_title_or_sku")
        self.assertEqual(received.get("product_fields", {}).get("sku"), self.ARTICLE_HEADER_SKU)


@unittest.skipUnless(_SDK_AVAILABLE, _LIVE_SKIP_REASON)
class ManagedAgentShowWritePlanDelegationTests(unittest.IsolatedAsyncioTestCase):
    """Production defect closure #4 -- REAL Railway evidence after #81:

    for a SHOW_WRITE_PLAN follow-up turn ("покажи финальный план записи
    ..."), Railway proved:

        MANAGED_PRODUCT_SELECTED tool=explain_bitrix_write_plan
            has_sku=True has_name=True
        PRODUCT_PREPARATION_DELEGATION_STARTED
        PRODUCT_PREPARATION_DELEGATION_SUCCEEDED

    yet the user-visible response was still the generic prepared-card
    preview, not a deterministic Bitrix/Aspro write plan. Root cause:
    ``select_product`` (PREPARE_PRODUCT) and ``explain_bitrix_write_plan``
    (SHOW_WRITE_PLAN) -- two DISTINCT semantic actions -- were both
    delegated into the exact same ``prepare_complete_card`` function,
    which always renders ``format_combined_preview_text``, never the
    write-plan text. This class proves the corrected dispatch: SHOW_
    WRITE_PLAN now delegates into the EXISTING, unmodified
    ``product_enrichment_bridge.format_write_plan_text`` renderer (the
    SAME one ``WorkflowPandaConversationGateway._explain_bitrix_write_
    plan`` already uses for the legacy conversational path) instead.

    CRITICAL ACCEPTANCE ASSERTION: uses the ACTUAL real ``ManagedAgentPOC``
    serialized tool-call contract, never a hand-typed guess. A throwaway
    ``ManagedAgentPOC`` instance (scripted only in which tool the MODEL
    decides to call, via the SDK's own no-API-key ``ScriptedModel`` --
    the tool's OWN business logic, ``select_product``/``explain_bitrix_
    write_plan``/``_row_product_fields``, executes for REAL against a
    real ingested XLSX row) captures the literal ``tool_calls`` shape
    for BOTH turns; those REAL, captured dicts -- never hand-crafted --
    are then replayed through the FULL, real
    ``WorkflowPandaConversationGateway.respond()`` entry point (only
    ``ManagedAgentPOC.run_turn`` itself is replaced, exactly as much of
    the boundary as every other test in this file replaces, so no live
    OpenAI call is required here)."""

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
                {f"{BRAND} {PRODUCT_A_SKU}": [fake_result(RESEARCH_URL_A, title=f"LG {PRODUCT_A_SKU}")]}
            ),
            scrape_fetch_handler=_scrape_fetch_handler,
            media_fetcher=FakeImageFetcher({IMAGE_URL_A: _png_bytes()}),
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

    def _capture_real_tool_calls(self, xlsx_path: str) -> tuple[list, list]:
        """Drives a throwaway, real ``ManagedAgentPOC`` (only the MODEL's
        tool-selection DECISION is scripted; ``select_product``/
        ``explain_bitrix_write_plan``/``_row_product_fields`` themselves
        run unmodified against the real ingested XLSX row) to capture the
        LITERAL ``tool_calls`` shape for turn 1 (select/prepare) and turn
        2 (show write plan), on the SAME conversation so turn 2's
        ``explain_bitrix_write_plan(identifier=None)`` genuinely resolves
        against turn 1's own persisted ``current_identifier`` -- never a
        hand-typed dict."""
        from managed_agent_poc.flags import FLAG_ENV_VAR

        old_flag = os.environ.get(FLAG_ENV_VAR)
        os.environ[FLAG_ENV_VAR] = "true"
        try:
            capture_poc = ManagedAgentPOC(
                dataset_store_path=os.path.join(self.tmp, "capture_dataset.sqlite3"),
                session_db_path=os.path.join(self.tmp, "capture_session.sqlite3"),
                state_store_path=os.path.join(self.tmp, "capture_state.sqlite3"),
            )
            turn1 = capture_poc.run_turn(
                text=PRODUCTION_TEXT,
                tenant_id="tenant-a",
                conversation_id="conv-write-plan-capture",
                artifact_bytes_path=xlsx_path,
                artifact_filename=FILENAME,
                test_scripted_plan=[
                    {"call_tool": "select_product", "arguments": {"identifier": None}},
                    {"final_output": "Товар подготовлен, в Bitrix не записан."},
                ],
            )
            assert turn1.status == "COMPLETED", turn1
            assert turn1.tool_calls[0]["output"]["status"] == "SELECTED", turn1.tool_calls
            turn2 = capture_poc.run_turn(
                text=WRITE_PLAN_TEXT,
                tenant_id="tenant-a",
                conversation_id="conv-write-plan-capture",
                test_scripted_plan=[
                    {"call_tool": "explain_bitrix_write_plan", "arguments": {"identifier": None}},
                    {"final_output": "План записи показан, ничего не записано."},
                ],
            )
            assert turn2.status == "COMPLETED", turn2
            assert turn2.tool_calls[0]["output"]["status"] == "WRITE_PLAN", turn2.tool_calls
        finally:
            if old_flag is None:
                os.environ.pop(FLAG_ENV_VAR, None)
            else:
                os.environ[FLAG_ENV_VAR] = old_flag
        return turn1.tool_calls, turn2.tool_calls

    async def test_prepare_then_show_write_plan_delegates_to_existing_write_plan_capability(self):
        artifact_id = await _register_upload(
            self.artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="conv-write-plan-1",
            filename=FILENAME,
            content=_xlsx_bytes(),
        )
        xlsx_path = os.path.join(self.tmp, "capture_" + FILENAME)
        with open(xlsx_path, "wb") as fh:
            fh.write(_xlsx_bytes())

        real_tool_calls_turn1, real_tool_calls_turn2 = self._capture_real_tool_calls(xlsx_path)
        real_sku = real_tool_calls_turn1[0]["output"]["sku"]
        self.assertEqual(real_sku, PRODUCT_A_SKU)
        self.assertEqual(real_tool_calls_turn2[0]["tool"], "explain_bitrix_write_plan")
        self.assertEqual(real_tool_calls_turn2[0]["output"]["would_write"]["sku"], PRODUCT_A_SKU)

        plan = [
            {"current_identifier": PRODUCT_A_SKU, "tool_calls": real_tool_calls_turn1, "final_output": "irrelevant-1"},
            {"current_identifier": PRODUCT_A_SKU, "tool_calls": real_tool_calls_turn2, "final_output": "irrelevant-2"},
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            # TURN 1 -- prepares the FULL card; asserts the fixture's
            # SKU/EAN/descriptions/characteristics/main image/gallery are
            # all present in the prepared state.
            r1 = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-1",
                    conversation_id="conv-write-plan-1",
                    attachment_refs=(artifact_id,),
                )
            )
            self.assertEqual(r1.metadata.get("managed_agent_tool"), "select_product")
            self.assertEqual(r1.metadata.get("delegated_to"), "product_enrichment_bridge.prepare_complete_card")
            self.assertIn(PRODUCT_A_SKU, r1.text)
            self.assertIn(PRODUCT_A_EAN, r1.text)
            self.assertIn("Характеристики:", r1.text)
            self.assertNotIn("Характеристики: 0", r1.text)
            self.assertIn("Главное изображение подготовлено: да", r1.text)

            calls_before_turn2 = len(self.transport.calls)

            # TURN 2 -- the REAL captured ``explain_bitrix_write_plan``
            # tool call now must produce a genuine deterministic
            # controlled Bitrix write plan, not the full-card preview.
            r2 = await self.panda.respond(
                ConversationRequest(
                    text=WRITE_PLAN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="req-2",
                    conversation_id="conv-write-plan-1",
                )
            )
            calls_after_turn2 = len(self.transport.calls)

        self.assertEqual(r2.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertEqual(r2.metadata.get("managed_agent_tool"), "explain_bitrix_write_plan")
        # Delegated into the EXISTING deterministic write-plan renderer,
        # never ``prepare_complete_card``'s own combined full-card preview.
        self.assertEqual(r2.metadata.get("delegated_to"), "product_enrichment_bridge.format_write_plan_text")
        self.assertEqual(r2.metadata.get("preparation_status"), "PREPARED")

        text = r2.text
        # This is a WRITE PLAN, not the generic ``format_combined_preview_
        # text``/full-card rendering (that text always starts with
        # "ПОДГОТОВЛЕННАЯ КАРТОЧКА"/enrichment-preview wording -- see
        # ``product_enrichment.preview.format_enrichment_preview_text``).
        self.assertIn("ЧТО БУДЕТ ЗАПИСАНО В BITRIX/ASPRO", text)
        self.assertNotIn("format_combined_preview_text", text)

        # SIMPLE_PRODUCT (#77) present, never SKU_WITH_OFFER.
        write_preview = r2.metadata.get("bitrix_write_preview") or {}
        self.assertEqual(write_preview.get("product_model", {}).get("model"), "SIMPLE_PRODUCT")
        self.assertIn("обычный товар без торговых предложений", text)

        # Exact resolved section ID present (existing category resolver,
        # never invented).
        self.assertEqual(write_preview.get("target_product", {}).get("resolved_section_id"), TV_SECTION_ID)
        self.assertIn(f"ID {TV_SECTION_ID}", text)

        # Purchase price and the EXISTING deterministic retail price
        # (the fixture's own "розница" column -- never a new coefficient,
        # never a hard-coded/model-generated price) both present.
        self.assertIn(PRODUCT_A_PURCHASE_PRICE, text)
        self.assertIn(PRODUCT_A_RETAIL_PRICE, text)
        self.assertNotIn("коэффициент", text.lower())

        # Verified writable fields listed (identity/pricing/category/
        # content/media/characteristics), and #77's unmapped fields
        # (EAN, sku/article on a SIMPLE_PRODUCT) reported HONESTLY --
        # never a false "all fields mapped"/"Нет таких полей".
        self.assertIn("Поля записи (по текущей политике записи):", text)
        self.assertIn("НЕ будет записано: sku", text)
        self.assertIn("НЕ будет записано: ean", text)
        self.assertNotIn("Нет таких полей", text)

        # Product Enrichment was NOT unnecessarily re-run: turn 2 added
        # exactly ONE new Bitrix call (the read-only ``catalog.section.
        # list`` ``prepare_single_product_write`` itself always performs),
        # and zero new research/media network calls -- the SAME
        # ``EnrichmentCache`` instance turn 1 already populated is reused
        # (cache hit), so no repeated LG product-page fetch or image
        # download happens for turn 2.
        self.assertEqual(calls_after_turn2 - calls_before_turn2, 1)
        self.assertEqual([m for m, _ in self.transport.calls][-1], "catalog.section.list")

        # Zero real Bitrix mutation across BOTH turns.
        methods_called = [m for m, _ in self.transport.calls]
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)
        self.assertFalse(r1.metadata.get("mutated"))
        self.assertFalse(r2.metadata.get("mutated"))


@unittest.skipUnless(_SDK_AVAILABLE and _HAS_KEY, _LIVE_SKIP_REASON)
class LiveRealSubprocessDelegationTests(unittest.IsolatedAsyncioTestCase):
    """Production defect closure #2 -- the strongest possible version of
    the "critical acceptance assertion": a REAL, non-mocked
    ``ManagedAgentPOC.run_turn()`` call (genuine OpenAI Agents SDK
    subprocess, real model) through the FULL, real
    ``WorkflowPandaConversationGateway.respond()`` entry point, proving
    the delegation boundary against the actual runtime contract rather
    than any hand-crafted or previously-captured double. Skips cleanly
    (never errors) when the isolated SDK is not installed or no
    ``OPENAI_API_KEY`` is set, exactly like ``tests/test_panda_managed_
    agent_integration.py`` already does for the same reason. Zero real
    Bitrix mutation (mocked HTTP transport, same as every other test in
    this file); zero real web/media network calls (no search provider,
    ``FakeImageFetcher`` with no fixtures -- only the OpenAI model call
    itself is real)."""

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
            search_provider=FakeSearchProvider({}),
            media_fetcher=FakeImageFetcher({}),
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

    async def test_live_production_request_delegates_to_existing_preparation_pipeline(self):
        """The EXACT real production first-turn request/attachment shape
        (a price list with sku/product_name/category/brand/ean/
        purchase_price columns and NO distinct retail-price column --
        the shape verified to reproduce the reported production symptom
        when delegation is bypassed) driven through a REAL model call,
        asserting the final response is the deterministic pipeline's own
        rendering, never the model's raw free-form synthesis."""
        wb = Workbook()
        ws = wb.active
        ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price"])
        ws.append([PRODUCT_A_SKU, PRODUCT_A_NAME, CATEGORY, BRAND, PRODUCT_A_EAN, PRODUCT_A_PURCHASE_PRICE])
        buf = io.BytesIO()
        wb.save(buf)

        artifact_id = await _register_upload(
            self.artifact_service, tenant="tenant-a", owner="u1", conv="conv-live-1", filename=FILENAME, content=buf.getvalue()
        )
        result = await self.panda.respond(
            ConversationRequest(
                text=PRODUCTION_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="req-1",
                conversation_id="conv-live-1",
                attachment_refs=(artifact_id,),
            )
        )

        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertIn(result.metadata.get("managed_agent_tool"), ("select_product", "explain_bitrix_write_plan"))
        self.assertFalse(result.metadata.get("mutated"))
        # Either the deterministic pipeline prepared the card, or it
        # failed and reported an EXPLICIT, honest failure -- either way,
        # the raw model's own free-form synthesis (asking the user for a
        # pricing coefficient, claiming "Нет таких полей") must never be
        # silently presented as the completed card.
        self.assertIn(result.metadata.get("preparation_status"), ("PREPARED", "FAILED"))
        if result.metadata.get("preparation_status") == "PREPARED":
            self.assertEqual(
                result.metadata.get("delegated_to"), "product_enrichment_bridge.prepare_complete_card"
            )
            self.assertIn(PRODUCT_A_SKU, result.text)
            self.assertIn(PRODUCT_A_EAN, result.text)
        else:
            self.assertIn("НЕ ЗАВЕРШЕНА", result.text)
            self.assertIn(PRODUCT_A_SKU, result.text)
        self.assertNotIn("коэффициент", result.text.lower())
        self.assertNotIn("Нет таких полей", result.text)
        self.assertNotIn("catalog.product.add", [m for m, _ in self.transport.calls])
        self.assertEqual(self.transport.product_add_count, 0)


if __name__ == "__main__":
    unittest.main()
