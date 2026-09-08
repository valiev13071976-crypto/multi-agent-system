"""PANDA — PRODUCTION HOTFIX: XLSX follow-up context only.

PR #38 fixed continuation reuse for a narrow set of follow-up phrasings
("Продолжай и выполни мой предыдущий запрос полностью") by merging the
prior substantive instruction into the tool call text -- but only when
``business_assistant.follow_up.resolve_follow_up`` classified the turn as
needing injected context (``inject_context=True``). That classification is
a hand-picked regex set (deictic/"продолжай"/short "главное" follow-ups)
and does NOT cover other completely legitimate continuation rephrasings,
e.g.:

    "Покажи подготовленную карточку полностью, включая закупочную цену
    из загруженного файла"

That phrasing matches none of ``resolve_follow_up``'s special-case regexes,
so ``inject_context`` stayed False, the prior instruction (and the SKU it
named) was dropped, and the second turn fell back to the same dimension-
only summary the first turn used to produce before PR #37/#38.

Root cause: ``business_assistant/action_continuation.py``'s FAMILY_EXCEL
continuation branch gated the prior-instruction merge on
``follow_up.inject_context`` -- but ``resolve_follow_up`` already populates
``FollowUpResolution.previous_user`` (the real, verbatim previous user
turn) on EVERY code path, regardless of ``kind``/``inject_context``. The
fix removes that unnecessarily narrow gate: any turn that is already an
established FAMILY_EXCEL continuation (``not is_new``) and has a real
previous user turn in history now gets the merged text, independent of
which follow-up regex (if any) matched.

No LLM, no new XLSX parser, no write/publish path, no batch-routing change.
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
TARGET_PURCHASE_PRICE = "22513.70"
USER_RETAIL_PRICE_RUB = "29990"

FIRST_TURN_TEXT = (
    "Проанализируй загруженный прайс. Цены в файле — закупочные. "
    "Пока ничего не публикуй на сайт. "
    f"Найди товар LG {TARGET_SKU} и подготовь его для добавления в Bitrix/Aspro Premier. "
    f"Розничная цена {USER_RETAIL_PRICE_RUB} \u20bd. "
    "Сначала покажи мне подготовленную карточку и план действий перед записью."
)

# Deliberately does NOT match any of resolve_follow_up's special-case
# regexes (no "продолжай", no deictic "это/этот", no "главное"/"короче")
# -- this is exactly the previously-unhandled rephrasing from production.
SECOND_TURN_TEXT = "Покажи подготовленную карточку полностью, включая закупочную цену из загруженного файла"


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
            ["sku", "product_name", "purchase_price"],
            ["SAM-A54", "Galaxy A54", "18000.00"],
            [TARGET_SKU, "LG 32LQ63006LA.ARUG TV", TARGET_PURCHASE_PRICE],
            ["APL-14", "iPhone 14", "70000.00"],
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


class XlsxFollowUpContextHotfixTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_full_reproduction_second_turn_reuses_context_without_reupload(self):
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

        # (1) first turn with XLSX still finds the requested row -- this
        # already-working behaviour must not change.
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
        self.assertIn(USER_RETAIL_PRICE_RUB, first.text)
        self.assertIn("не выполнена", first.text)

        # (2) second turn, WITHOUT re-uploading the attachment, reuses the
        # existing dataset context (only prior conversation history is
        # supplied -- no attachment_refs).
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
        # Must NOT regress to the dimension-only fallback.
        self.assertNotIn("столбцов.", second.text)
        # (3) a real field/value from that row is retrieved.
        self.assertIn(TARGET_SKU, second.text)
        self.assertIn(TARGET_PURCHASE_PRICE, second.text)
        # (4) the prior user-supplied retail price remains available.
        self.assertIn(USER_RETAIL_PRICE_RUB, second.text)
        # (5) preview/no-write remains enforced.
        self.assertIn("не выполнена", second.text)
        self.assertEqual(second.metadata.get("action_decision"), CALL_TOOL)
        self.assertEqual(second.metadata.get("artifacts"), [])


if __name__ == "__main__":
    unittest.main()
