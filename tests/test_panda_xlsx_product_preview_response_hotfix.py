"""PANDA — PRODUCTION HOTFIX: XLSX product-preview response only.

After PR #37/#38/#39 the small-XLSX product lookup/follow-up flow reliably
locates the requested row and keeps both the file's purchase price and the
user-supplied retail price alive across turns. The remaining defect was in
the FINAL rendering of that already-resolved data: ``data_intel.service
.DataIntelligenceService._row_lookup_result`` (added by PR #38) only ever
emitted a *promissory* sentence --

    "Подготовила карточку товара и план действий для предпросмотра.
     Публикация/запись не выполнена — жду вашего подтверждения."

-- which *talks about* having prepared a card/plan without ever actually
showing the card's fields. That is exactly the "implementation/planning
prose instead of a concrete user-facing product preview" defect: a status
sentence about a plan, not a product card.

Root cause: ``_row_lookup_result`` collected only price columns into
``summary_text``; it never rendered the row's other resolved fields
(name/SKU/EAN/category/brand/stock) as a card, and the closing sentence
described an abstract "plan" rather than presenting concrete field values.

Fix (minimum, in ``data_intel/service.py``): ``_row_lookup_result`` now
renders a "Карточка товара (предпросмотр):" block with one bullet per
schema role that is ACTUALLY present in this workbook (name, SKU/article,
EAN, category, brand, purchase price, resolved retail price, stock),
skipping any role absent from the sheet -- never inventing values -- and
ends with a short, concrete action-status line (prepared for Bitrix/Aspro
preview; not published; not written; awaiting confirmation) instead of
vague planning prose.

No LLM, no new XLSX parser, no write/publish path, no batch-routing or
follow-up-merge change (PR #37/#38/#39 behaviour is asserted unchanged).
"""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CALL_TOOL
from business_assistant.conversation_gateway import (
    ConversationRequest,
    WorkflowPandaConversationGateway,
)
from business_assistant.follow_up import HistoryTurn
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

TARGET_SKU = "32LQ63006LA.ARUG"
TARGET_PRODUCT_NAME = "LG 32LQ63006LA.ARUG TV"
TARGET_CATEGORY = "Televisions"
TARGET_BRAND = "LG"
TARGET_PURCHASE_PRICE = "22513.70"
TARGET_STOCK = "7"
USER_RETAIL_PRICE_RUB = "29990"

FIRST_TURN_TEXT = (
    "Проанализируй загруженный прайс. Цены в файле — закупочные. "
    "Пока ничего не публикуй на сайт. "
    f"Найди товар LG {TARGET_SKU} и подготовь его для добавления в Bitrix/Aspro Premier. "
    f"Розничная цена {USER_RETAIL_PRICE_RUB} \u20bd. "
    "Сначала покажи мне подготовленную карточку и план действий перед записью."
)

SECOND_TURN_TEXT = (
    "Покажи подготовленную карточку полностью, включая закупочную цену из загруженного файла"
)

# Vocabulary that is only ever appropriate in developer-facing prose
# (implementation plans, testing/logging advice) -- must never leak into a
# customer-facing product-preview reply.
_IMPLEMENTATION_PROSE_MARKERS = (
    "чтобы это гарантировать",
    "в каждом режиме работы",
    "проверяем логи",
    "тестируем",
    "логирование",
    "test coverage",
    "unit test",
    "regression test",
)


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _price_list_bytes() -> bytes:
    return _xlsx_bytes(
        [
            ["sku", "product_name", "category", "brand", "purchase_price", "stock"],
            ["SAM-A54", "Galaxy A54", "Phones", "Samsung", "18000.00", "12"],
            [
                TARGET_SKU,
                TARGET_PRODUCT_NAME,
                TARGET_CATEGORY,
                TARGET_BRAND,
                TARGET_PURCHASE_PRICE,
                TARGET_STOCK,
            ],
            ["APL-14", "iPhone 14", "Phones", "Apple", "70000.00", "3"],
        ]
    )


def _excel_gateway():
    svc = DataIntelligenceService(InMemoryDatasetStore())
    artifact_service = ArtifactService(store=InMemoryArtifactStore())
    svc.artifact_service = artifact_service
    registry = ToolRegistry()
    register_platform_tools(registry, data_intelligence=svc)
    gateway = ToolGateway(registry=registry, register_search=False)
    return gateway, svc, artifact_service


class XlsxProductPreviewResponseHotfixTests(unittest.IsolatedAsyncioTestCase):
    def _panda(self, gateway, artifact_service):
        return WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=gateway,
            artifact_service=artifact_service,
        )

    async def _register_upload(self, artifact_service, *, tenant, owner, conv, filename, content):
        rec = artifact_service.register_upload(
            tenant_id=tenant, owner_id=owner, filename=filename, content=content
        )
        artifact_service.attach_to_conversation(
            tenant_id=tenant, artifact_id=rec.artifact_id, conversation_id=conv
        )
        return rec.artifact_id

    async def test_product_preview_renders_concrete_card_not_planning_prose(self):
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._panda(gateway, artifact_service)
        ref = await self._register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_price_list_bytes(),
        )

        # First turn: already-working find-and-preview behaviour (PR #37/#38).
        first = await panda.respond(
            ConversationRequest(
                text=FIRST_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        self.assertIn(TARGET_SKU, first.text)
        self.assertIn(TARGET_PURCHASE_PRICE, first.text)
        self.assertIn(USER_RETAIL_PRICE_RUB, first.text)
        self.assertIn("не выполнена", first.text)
        for marker in _IMPLEMENTATION_PROSE_MARKERS:
            self.assertNotIn(marker, first.text.lower())

        # Second turn: follow-up asking to SEE the card (PR #39 context
        # reuse), without re-uploading the attachment.
        history = (
            HistoryTurn(role="user", content=FIRST_TURN_TEXT),
            HistoryTurn(role="assistant", content=first.text),
        )
        second = await panda.respond(
            ConversationRequest(
                text=SECOND_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
                history=history,
                attachment_refs=(),
            )
        )
        text = second.text

        # (1) actual row values, not only workbook dimensions.
        self.assertNotIn("столбцов.", text)
        self.assertIn(TARGET_SKU, text)
        self.assertIn(TARGET_PRODUCT_NAME, text)
        # (2) purchase price from the file.
        self.assertIn(TARGET_PURCHASE_PRICE, text)
        # (3) user-provided retail price preserved.
        self.assertIn(USER_RETAIL_PRICE_RUB, text)
        # (4) other available row fields rendered dynamically.
        self.assertIn(TARGET_CATEGORY, text)
        self.assertIn(TARGET_BRAND, text)
        self.assertIn(TARGET_STOCK, text)
        # (5) no implementation-plan/debug-style prose.
        lowered = text.lower()
        for marker in _IMPLEMENTATION_PROSE_MARKERS:
            self.assertNotIn(marker, lowered)
        # A concrete action-status footer replaces the old vague sentence.
        self.assertIn("не выполнена", text)
        self.assertNotIn("план действий", lowered)

        # (6) no Bitrix write/publish occurred: this stays a deterministic
        # CALL_TOOL preview turn, and the tool call produced no artifacts
        # (i.e. no write side effect was scheduled).
        self.assertEqual(second.metadata.get("action_decision"), CALL_TOOL)
        self.assertEqual(second.metadata.get("artifacts"), [])

    async def test_missing_optional_field_is_omitted_not_invented(self):
        # A leaner workbook without category/brand/stock columns must still
        # produce a valid card -- omitting absent fields, never fabricating
        # them.
        gateway, svc, artifact_service = _excel_gateway()
        panda = self._panda(gateway, artifact_service)
        lean_bytes = _xlsx_bytes(
            [
                ["sku", "product_name", "purchase_price"],
                ["SAM-A54", "Galaxy A54", "18000.00"],
                [TARGET_SKU, TARGET_PRODUCT_NAME, TARGET_PURCHASE_PRICE],
            ]
        )
        ref = await self._register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c2",
            filename="lean.xlsx",
            content=lean_bytes,
        )
        result = await panda.respond(
            ConversationRequest(
                text=(
                    f"Найди товар {TARGET_SKU}, розничная цена {USER_RETAIL_PRICE_RUB} \u20bd, "
                    "покажи карточку"
                ),
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c2",
                attachment_refs=(ref,),
            )
        )
        text = result.text
        self.assertIn(TARGET_SKU, text)
        self.assertIn(TARGET_PURCHASE_PRICE, text)
        self.assertNotIn("Категория:", text)
        self.assertNotIn("Бренд:", text)
        self.assertNotIn("Остаток:", text)
        for marker in _IMPLEMENTATION_PROSE_MARKERS:
            self.assertNotIn(marker, text.lower())


if __name__ == "__main__":
    unittest.main()
