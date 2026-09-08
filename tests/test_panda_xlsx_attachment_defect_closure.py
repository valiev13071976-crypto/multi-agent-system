"""PANDA — PRODUCTION DEFECT CLOSURE: Business Assistant XLSX attachment -> failed response.

Regression coverage for the reproduced production defect where a valid XLSX
attachment plus a business-flavored natural-language query (mentioning
"Bitrix"/"подготовь") got misrouted to the attachment-blind legacy
Business Workflow engine (``business_assistant.service.BusinessAssistantService
.execute``), which then hit a ``BLOCKED`` capability-unavailable step and
returned an empty ``ex.summary``. The empty summary was indistinguishable,
on the frontend, from "Panda produced no answer"
(``static/shared/presentation.js``'s ``isInternalMetadata("") === true``),
so the UI showed the generic "Panda не смогла сформировать ответ.
Попробуйте ещё раз." message despite every HTTP call returning 200.

Three independent regressions are pinned here, matching the three points in
the fix:

1. ``business_assistant.intent.classify_intent`` must become attachment-aware
   so a request carrying a file attachment (without an explicit write/publish
   verb) is routed to the conversational pipeline -- the only pipeline that
   actually resolves ``artifact_refs`` and reads file content.
2. ``business_assistant.service.BusinessAssistantService._finalize_status``
   must always populate ``ex.summary`` for every terminal state the legacy
   engine can end in (not only the "everything COMPLETED" branch), so a
   partially-blocked business workflow still returns a non-empty, honest
   answer instead of silently masking real findings behind "".
3. ``business_assistant_api.service.BusinessAssistantApiService`` must keep
   forwarding the new attachment preference end-to-end (submit/submit_async)
   while preserving the existing large-batch Excel routing (unaffected,
   still uses the fixture-seeded batch workflow engine, never the
   conversational path).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import unittest

from business_assistant.conversation_gateway import FakePandaConversationGateway
from business_assistant.errors import BA_CAPABILITY_UNAVAILABLE
from business_assistant.intent import classify_intent, is_conversational
from business_assistant.models import INTENT_CONVERSATIONAL
from business_assistant.service import BusinessAssistantService
from business_assistant_api.models import ST_COMPLETED, WORKLOAD_BATCH, WORKLOAD_INTERACTIVE
from business_assistant_api.runtime import build_business_assistant_api_runtime
from marketplace.service import MarketplacePlatformService

# The exact reproduced production request text (LG_TV.xlsx attachment case).
REPRO_TEXT = (
    "Проанализируй загруженный прайс. Цены в файле — закупочные. "
    "Пока ничего не публикуй на сайт. Найди товар LG 32LQ63006LA.ARUG и "
    "подготовь его для добавления в Bitrix/Aspro Premier. "
    "Розничная цена 29 990 \u20bd. Сначала покажи мне подготовленную карточку "
    "и план действий перед записью."
)

FAKE_REPLY = "1 rows, 4 columns. Price (Purchase price): from 15000 to 15000, average 15000."


def _fake_gateway(calls=None):
    return FakePandaConversationGateway(response=FAKE_REPLY, calls=calls if calls is not None else [])


class IntentAttachmentAwareRoutingTests(unittest.TestCase):
    """Pins fix #1: attachment presence must steer routing decisions."""

    def test_repro_text_with_attachment_is_conversational(self):
        self.assertTrue(is_conversational(REPRO_TEXT, has_attachments=True))
        self.assertEqual(classify_intent(REPRO_TEXT, has_attachments=True), INTENT_CONVERSATIONAL)

    def test_repro_text_without_attachment_stays_business(self):
        # Same wording, no attachment: must NOT be forced conversational --
        # only presence of a real attachment changes the routing decision.
        self.assertNotEqual(classify_intent(REPRO_TEXT, has_attachments=False), INTENT_CONVERSATIONAL)

    def test_attachment_with_explicit_publish_verb_stays_business(self):
        # A write/publish instruction must still win over the attachment
        # preference -- governed writes never get silently rerouted.
        text = "Опубликуй все товары из прайса на сайт"
        self.assertFalse(is_conversational(text, has_attachments=True))
        self.assertNotEqual(classify_intent(text, has_attachments=True), INTENT_CONVERSATIONAL)

    def test_attachment_default_still_conversational_backward_compatible(self):
        # has_attachments defaults False; existing text-only call sites are
        # unaffected by the new parameter.
        self.assertEqual(classify_intent("привет"), INTENT_CONVERSATIONAL)


class FinalizeStatusSummaryTests(unittest.TestCase):
    """Pins fix #2: every terminal state carries a non-empty summary."""

    def _svc(self) -> BusinessAssistantService:
        return BusinessAssistantService(marketplace=MarketplacePlatformService())

    def test_blocked_capability_still_produces_summary(self):
        svc = self._svc()
        req = svc.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Найди письмо поставщика и подготовь ответ",
        )
        plan = svc.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = svc.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        blocked = [s for s in ex.steps.values() if s.error_code == BA_CAPABILITY_UNAVAILABLE]
        self.assertTrue(blocked, "fixture scenario must still reproduce a BLOCKED capability step")
        # The actual defect: this used to be "" for any BLOCKED-but-not a
        # required-write terminal outcome, which the frontend then rendered
        # as "Panda could not generate a response".
        self.assertTrue(str(ex.summary or "").strip(), "ex.summary must never be empty on a terminal status")


class ApiAttachmentRoutingClosureTests(unittest.TestCase):
    """Pins fix #3: end-to-end API routing + non-empty usable final answer."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_xlsx_defect.sqlite")
        self.gateway_calls: list = []
        self.gateway = _fake_gateway(self.gateway_calls)
        self.rt = build_business_assistant_api_runtime(
            db_path=self.db,
            conversation_gateway=self.gateway,
        )
        self.svc = self.rt.service

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_xlsx_attachment_with_business_wording_reaches_conversational_pipeline(self):
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=REPRO_TEXT,
            artifact_refs=["artifact://upload/LG_TV.xlsx"],
            idempotency_key="xlsx-defect-1",
        )
        # Not a large-batch dataset (single-row style repro): must be routed
        # interactively, straight into the conversational pipeline that can
        # actually resolve the attachment -- never the attachment-blind
        # legacy business-workflow engine.
        self.assertEqual(rec.workload_class, WORKLOAD_INTERACTIVE)
        self.assertEqual(rec.status, ST_COMPLETED)
        self.assertEqual(len(self.gateway_calls), 1)
        self.assertIn("artifact://upload/LG_TV.xlsx", self.gateway_calls[0].attachment_refs)

        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id)
        # The defect surfaced as an empty/failure-shaped summary despite
        # HTTP 200; assert a real, non-empty, usable answer instead.
        self.assertEqual(result["summary"], FAKE_REPLY)
        self.assertTrue(result["summary"].strip())

    def test_batch_excel_attachment_still_bypasses_conversational_pipeline(self):
        # Existing large-batch Excel routing (Block 5.1/5.2 scale-safe path)
        # must be completely unaffected by the new attachment preference.
        rec = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message="Сравни закупку с текущими ценами и подготовь итоговую Excel таблицу.",
            artifact_refs=["artifact://excel/price-list.xlsx"],
            idempotency_key="xlsx-defect-batch-1",
        )
        self.assertEqual(rec.workload_class, WORKLOAD_BATCH)
        self.assertEqual(self.gateway_calls, [])


class ObservabilityDiagnosticLogTests(unittest.TestCase):
    """Observability requirement: a degraded/empty terminal outcome must be
    correlatable by request_id from logs alone, without another
    reproduction round."""

    def test_blocked_capability_emits_diagnostic_log(self):
        rt = build_business_assistant_api_runtime(
            db_path=os.path.join(tempfile.mkdtemp(), "ba_xlsx_diag.sqlite"),
            with_integration=False,
        )
        try:
            with self.assertLogs("business_assistant_api.diagnostics", level="INFO") as ctx:
                rec = rt.service.submit(
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    message="Найди письмо поставщика и подготовь ответ",
                    idempotency_key="xlsx-diag-1",
                )
            self.assertTrue(rec.request_id)
            joined = "\n".join(ctx.output)
            self.assertIn("business_workflow_degraded_or_empty_result", joined)
            self.assertIn(rec.request_id, joined)
        finally:
            rt.close()


if __name__ == "__main__":
    unittest.main()
