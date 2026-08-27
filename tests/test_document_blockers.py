"""Blocker fixes: scanned PDF OCR end-to-end + large-document workflow."""

from __future__ import annotations

import asyncio
import io
import unittest
import uuid

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from documents.errors import (
    DOCUMENT_REQUIRES_OCR,
    OCR_FAILED,
    OCR_UNAVAILABLE,
    PDF_RASTERIZATION_UNAVAILABLE,
    DocumentError,
)
from documents.intelligence.extraction import extract_structured
from documents.intelligence.large import LargeDocumentPolicy
from documents.intelligence.ocr import FakeOCRProvider, NullOCRProvider
from documents.intelligence.pdf_ocr import build_pdf_document_content
from documents.intelligence.raster import FakePdfRasterizer, NullPdfRasterizer
from documents.intelligence.service import DocumentIntelligenceService
from documents.intelligence.workflow_def import register_document_workflows
from documents.models import SOURCE_TEST_FIXTURE, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL
from security.tenant import normalize_tenant_id
from workflow.builtins import register_builtin_definitions
from workflow.definition import StepResult, STEP_TYPE_HANDLER
from workflow.models import STATUS_COMPLETED, STATUS_FAILED
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager
from workflow.store import InMemoryWorkflowStateStore


def _scope(sid="proj-blk", tenant="tenant-a"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid, tenant_ref=tenant)


def _blank_pdf(pages: int = 1) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _text_pdf(text: str = "Hello text layer") -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    page = w.pages[0]
    stream = DecodedStreamObject()
    content = f"BT /F1 12 Tf 50 150 Td ({text}) Tj ET".encode("latin-1")
    stream.set_data(content)
    stream[NameObject("/Length")] = NumberObject(len(content))
    page[NameObject("/Contents")] = stream
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _mixed_pdf() -> bytes:
    """Page1 text, page2 blank (needs OCR)."""
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_blank_page(width=200, height=200)
    page = w.pages[0]
    stream = DecodedStreamObject()
    content = b"BT /F1 12 Tf 50 150 Td (Invoice No MIX-1) Tj ET"
    stream.set_data(content)
    stream[NameObject("/Length")] = NumberObject(len(content))
    page[NameObject("/Contents")] = stream
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class FailingOCR(FakeOCRProvider):
    available = True

    def recognize(self, data: bytes, *, filename: str = "") -> dict:
        raise DocumentError(OCR_FAILED)


class ScannedPdfOcrTests(unittest.TestCase):
    def test_text_pdf_no_rasterize(self):
        calls = {"n": 0}

        class CountingRaster(FakePdfRasterizer):
            def rasterize(self, pdf_bytes, *, pages=None, scale=2.0):
                calls["n"] += 1
                return super().rasterize(pdf_bytes, pages=pages, scale=scale)

        intel = DocumentIntelligenceService(
            ocr_provider=FakeOCRProvider("SHOULD_NOT_APPEAR"),
            rasterizer=CountingRaster(),
        )
        content = intel.extract_pdf_with_ocr_fallback(
            document_id="t1",
            data=_text_pdf("Readable Layer Text"),
            filename="ok.pdf",
        )
        self.assertEqual(calls["n"], 0)
        self.assertEqual(content.extraction_method, "pdf_text")
        self.assertNotIn("SHOULD_NOT_APPEAR", content.text)

    def test_scanned_pdf_end_to_end(self):
        intel = DocumentIntelligenceService(
            ocr_provider=FakeOCRProvider("OCR Invoice No SCAN-9 Total: 50"),
            rasterizer=FakePdfRasterizer(),
        )
        content = intel.extract_pdf_with_ocr_fallback(
            document_id="s1",
            data=_blank_pdf(2),
            filename="scan.pdf",
        )
        self.assertEqual(content.extraction_method, "pdf_ocr")
        self.assertIn("SCAN-9", content.text)
        self.assertEqual(len(content.pages), 2)
        self.assertTrue(all(p.get("extraction_method") == "ocr" for p in content.pages))
        structured = extract_structured(content, document_type="invoice")
        self.assertEqual(structured.document_type, "invoice")

    def test_mixed_pdf_preserves_order(self):
        intel = DocumentIntelligenceService(
            ocr_provider=FakeOCRProvider("OCR_PAGE_TWO"),
            rasterizer=FakePdfRasterizer(),
        )
        content = intel.extract_pdf_with_ocr_fallback(
            document_id="m1",
            data=_mixed_pdf(),
            filename="mixed.pdf",
        )
        self.assertEqual(content.extraction_method, "pdf_mixed_ocr")
        methods = [p.get("extraction_method") for p in content.pages]
        self.assertEqual(methods[0], "pdf_text")
        self.assertEqual(methods[1], "ocr")
        self.assertIn("MIX-1", content.text)
        self.assertIn("OCR_PAGE_TWO", content.text)
        # text page before OCR page in merged text
        self.assertLess(content.text.find("MIX-1"), content.text.find("OCR_PAGE_TWO"))

    def test_rasterizer_unavailable(self):
        intel = DocumentIntelligenceService(
            ocr_provider=FakeOCRProvider("x"),
            rasterizer=NullPdfRasterizer(),
        )
        with self.assertRaises(DocumentError) as ctx:
            intel.extract_pdf_with_ocr_fallback(
                document_id="r1", data=_blank_pdf(), filename="s.pdf"
            )
        self.assertEqual(ctx.exception.reason, PDF_RASTERIZATION_UNAVAILABLE)

    def test_ocr_unavailable(self):
        intel = DocumentIntelligenceService(
            ocr_provider=NullOCRProvider(),
            rasterizer=FakePdfRasterizer(),
        )
        with self.assertRaises(DocumentError) as ctx:
            intel.extract_pdf_with_ocr_fallback(
                document_id="o1", data=_blank_pdf(), filename="s.pdf"
            )
        self.assertIn(ctx.exception.reason, {DOCUMENT_REQUIRES_OCR, OCR_UNAVAILABLE})

    def test_ocr_failed(self):
        intel = DocumentIntelligenceService(
            ocr_provider=FailingOCR(),
            rasterizer=FakePdfRasterizer(),
        )
        with self.assertRaises(DocumentError) as ctx:
            intel.extract_pdf_with_ocr_fallback(
                document_id="f1", data=_blank_pdf(), filename="s.pdf"
            )
        self.assertEqual(ctx.exception.reason, OCR_FAILED)

    def test_document_service_uses_ocr_fallback(self):
        store = InMemoryDocumentStore()
        intel = DocumentIntelligenceService(
            ocr_provider=FakeOCRProvider("Service OCR CONTRACT No C-1"),
            rasterizer=FakePdfRasterizer(),
        )
        # Keep under large threshold
        svc = DocumentService(
            store,
            intelligence=intel,
            large_policy=LargeDocumentPolicy(max_sync_bytes=10_000_000, max_sync_pages=1000),
        )
        row = svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="scan.pdf",
                content=_blank_pdf(),
                source_type=SOURCE_TEST_FIXTURE,
                source_id="ocr-svc",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertEqual(row.status, "parsed")
        chunks = svc.list_chunks(row.document_id, requesting_scope=_scope())
        joined = " ".join(c.content_safe or "" for c in chunks)
        self.assertIn("C-1", joined)


class LargeDocumentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store = InMemoryWorkflowStateStore()
        self.sm = StateManager(store=store)
        self.bundle = build_workflow_runtime(state_manager=self.sm)
        register_builtin_definitions(self.bundle.definitions)

        async def _default(ctx):
            return StepResult(ok=True, data={"step_id": ctx["step"].step_id, "path": "left"})

        self.bundle.platform.register_handler(STEP_TYPE_HANDLER, _default)
        register_document_workflows(self.bundle.definitions, self.bundle.platform)

        self.doc_store = InMemoryDocumentStore()
        self.intel = DocumentIntelligenceService(
            ocr_provider=FakeOCRProvider("Large OCR Invoice No L-1"),
            rasterizer=FakePdfRasterizer(),
            workflow_runtime=self.bundle,
            large_policy=LargeDocumentPolicy(
                max_sync_bytes=100, max_sync_pages=1, pages_per_batch=2
            ),
        )
        self.svc = DocumentService(
            self.doc_store,
            intelligence=self.intel,
            large_policy=self.intel.large_policy,
            workflow_runtime=self.bundle,
        )
        # Engine facade for handlers
        class Engine:
            document_service = self.svc
            document_intelligence = self.intel

        self.bundle.platform.workflow_engine = Engine()

    async def test_under_threshold_sync(self):
        policy = LargeDocumentPolicy(max_sync_bytes=10_000_000, max_sync_pages=100)
        self.assertFalse(policy.requires_async(size_bytes=10, page_count=1))

    async def test_over_threshold_enqueues_not_sync_parse(self):
        data = _blank_pdf(3)  # pages > max_sync_pages=1
        # pad size too
        data = data + b"\x00" * 200
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="big.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="big1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertEqual(row.status, "ingested")
        self.assertEqual(row.metadata_safe.get("extraction_mode"), "async")
        self.assertTrue(row.metadata_safe.get("workflow_id"))
        self.assertEqual(row.chunk_count, 0)
        # blob retained for workers
        self.assertIsNotNone(self.doc_store.get_blob(row.document_id))

        # run worker until complete
        wf_id = row.metadata_safe["workflow_id"]
        for _ in range(40):
            await self.bundle.worker.run_once()
            st = self.sm.get(wf_id)
            if st.status == STATUS_COMPLETED:
                break
        st = self.sm.get(wf_id)
        self.assertEqual(st.status, STATUS_COMPLETED)
        final = self.doc_store.get(row.document_id)
        self.assertGreater(final.chunk_count, 0)
        self.assertEqual(final.metadata_safe.get("extraction_mode"), "async_complete")
        self.assertIsNone(self.doc_store.get_blob(row.document_id))

    async def test_idempotent_large_enqueue(self):
        data = _blank_pdf(5) + b"\x00" * 200
        req = DocumentIngestRequest(
            scope=_scope(),
            filename="idem.pdf",
            content=data,
            source_type=SOURCE_TEST_FIXTURE,
            source_id="idem1",
            sensitivity=SENSITIVITY_INTERNAL,
        )
        first = self.svc.ingest(req)
        # Second ingest dedupes by hash before enqueue — force enqueue twice via intelligence
        plan1 = self.intel.plan_large_extraction(
            document_id=first.document_id,
            tenant_id="tenant-a",
            page_count=5,
            size_bytes=10_000,
            enqueue=True,
        )
        plan2 = self.intel.plan_large_extraction(
            document_id=first.document_id,
            tenant_id="tenant-a",
            page_count=5,
            size_bytes=10_000,
            enqueue=True,
        )
        self.assertEqual(plan1["workflow_id"], plan2["workflow_id"])
        self.assertTrue(plan2.get("idempotent"))

    async def test_batches_bounded(self):
        plan = self.intel.plan_large_extraction(
            document_id="d",
            tenant_id="t",
            page_count=25,
            size_bytes=10_000_000,
        )
        self.assertTrue(plan["async"])
        self.assertEqual(plan["batch_count"], 13)  # pages_per_batch=2
        for b in plan["batches"]:
            self.assertLessEqual(b["page_end"] - b["page_start"] + 1, 2)

    async def test_tenant_isolation_on_status(self):
        data = _blank_pdf(3) + b"\x00" * 200
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(tenant="tenant-a"),
                filename="iso.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="iso1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        wf_id = row.metadata_safe["workflow_id"]
        # Tenant B cannot get document
        other = MemoryScope(scope_type=SCOPE_PROJECT, scope_id="proj-blk", tenant_ref="tenant-b")
        self.assertIsNone(self.svc.get(row.document_id, requesting_scope=other))
        # Workflow tenant scoped via execution key lookup
        found_b = self.sm.find_by_execution_key(
            row.metadata_safe["execution_key"],
            tenant_id=normalize_tenant_id("tenant-b"),
        )
        self.assertIsNone(found_b)
        found_a = self.sm.find_by_execution_key(
            row.metadata_safe["execution_key"],
            tenant_id=normalize_tenant_id("tenant-a"),
        )
        self.assertIsNotNone(found_a)
        self.assertEqual(found_a.workflow_id, wf_id)


class SpyRasterizer(FakePdfRasterizer):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[int, ...]] = []

    def rasterize(self, pdf_bytes, *, pages=None, scale=2.0):
        wanted = tuple(pages) if pages is not None else ()
        self.calls.append(wanted)
        return super().rasterize(pdf_bytes, pages=pages, scale=scale)


class BoundedBatchExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store = InMemoryWorkflowStateStore()
        self.sm = StateManager(store=store)
        self.bundle = build_workflow_runtime(state_manager=self.sm)
        register_builtin_definitions(self.bundle.definitions)

        async def _default(ctx):
            return StepResult(ok=True, data={"step_id": ctx["step"].step_id, "path": "left"})

        self.bundle.platform.register_handler(STEP_TYPE_HANDLER, _default)
        register_document_workflows(self.bundle.definitions, self.bundle.platform)

        self.spy = SpyRasterizer()
        self.doc_store = InMemoryDocumentStore()
        self.intel = DocumentIntelligenceService(
            ocr_provider=FakeOCRProvider("Bounded OCR text"),
            rasterizer=self.spy,
            workflow_runtime=self.bundle,
            large_policy=LargeDocumentPolicy(
                max_sync_bytes=50,
                max_sync_pages=5,
                pages_per_batch=5,
            ),
        )
        self.svc = DocumentService(
            self.doc_store,
            intelligence=self.intel,
            large_policy=self.intel.large_policy,
            workflow_runtime=self.bundle,
        )

        class Engine:
            document_service = self.svc
            document_intelligence = self.intel

        self.bundle.platform.workflow_engine = Engine()

    async def _run_to_complete(self, wf_id: str, *, max_ticks: int = 80):
        from datetime import timedelta

        from workflow.models import utc_now

        for _ in range(max_ticks):
            st = self.sm.get(wf_id)
            if st.status == STATUS_COMPLETED:
                return
            if st.status == STATUS_FAILED:
                return
            # Force due retries in tests (policy delay is tiny but not zero)
            if st.status == "retry_wait":
                self.sm.mark_retry_wait(
                    wf_id,
                    next_retry_at=utc_now() - timedelta(seconds=1),
                    error_code=st.error_code,
                )
            if hasattr(self.bundle, "reenqueue_due_retries"):
                self.bundle.reenqueue_due_retries()
            await self.bundle.worker.run_once()
        self.fail(f"workflow did not finish: {self.sm.get(wf_id).status}")

    async def test_20_page_pdf_exactly_4_bounded_raster_calls(self):
        data = _blank_pdf(20) + b"\x00" * 100
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="twenty.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="b20",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertEqual(row.metadata_safe.get("extraction_mode"), "async")
        wf_id = row.metadata_safe["workflow_id"]
        await self._run_to_complete(wf_id)
        self.assertEqual(self.sm.get(wf_id).status, STATUS_COMPLETED)
        # Exactly 4 rasterize calls; none with >5 pages; none with all 20
        self.assertEqual(len(self.spy.calls), 4)
        for call in self.spy.calls:
            self.assertLessEqual(len(call), 5)
            self.assertNotEqual(len(call), 20)
        # Union covers all 20 pages
        covered = sorted({p for call in self.spy.calls for p in call})
        self.assertEqual(covered, list(range(1, 21)))
        final = self.doc_store.get(row.document_id)
        self.assertGreater(final.chunk_count, 0)
        chunks = self.doc_store.list_chunks(row.document_id)
        pages = sorted(
            {
                int(c.metadata_safe.get("page") or 0)
                for c in chunks
                if c.source_location
            }
            | {int((c.source_location or "").rsplit(":", 1)[-1]) for c in chunks if "page:" in (c.source_location or "")}
        )
        # Provenance locations mention pages 1..20
        locs = " ".join(c.source_location or "" for c in chunks)
        for p in range(1, 21):
            self.assertIn(f"page:{p}", locs)

    async def test_restart_skips_completed_batches(self):
        data = _blank_pdf(15) + b"\x00" * 100
        self.intel.large_policy = LargeDocumentPolicy(
            max_sync_bytes=50, max_sync_pages=5, pages_per_batch=5
        )
        self.svc.large_policy = self.intel.large_policy
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="restart.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="rst",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        wf_id = row.metadata_safe["workflow_id"]
        self.sm.start(wf_id)
        # prepare + two extract slices (batches 0 and 1)
        await self.bundle.platform.advance(wf_id, max_steps=1)  # prepare
        await self.bundle.platform.advance(wf_id, max_steps=1)  # batch 0
        await self.bundle.platform.advance(wf_id, max_steps=1)  # batch 1
        partials = self.doc_store.list_extract_partials(row.document_id)
        completed_before = sorted(
            i for i, p in partials.items() if p.get("status") == "completed"
        )
        self.assertEqual(completed_before, [0, 1])
        calls_before = len(self.spy.calls)
        self.assertEqual(calls_before, 2)

        # Simulate restart mid-flight
        self.bundle.platform.recover_after_restart(wf_id)
        self.bundle.enqueue_existing(wf_id, idempotent=True)
        await self._run_to_complete(wf_id)
        self.assertEqual(self.sm.get(wf_id).status, STATUS_COMPLETED)
        # Only one additional raster call for batch 2
        self.assertEqual(len(self.spy.calls), 3)
        self.assertEqual(self.spy.calls[0], (1, 2, 3, 4, 5))
        self.assertEqual(self.spy.calls[1], (6, 7, 8, 9, 10))
        self.assertEqual(self.spy.calls[2], (11, 12, 13, 14, 15))

    async def test_failed_batch_retry_does_not_redo_completed(self):
        class FlakyOCR(FakeOCRProvider):
            def __init__(self):
                super().__init__("ok")
                self.n = 0

            def recognize(self, data: bytes, *, filename: str = "") -> dict:
                self.n += 1
                # Fail once on first page of batch 2 (after 10 OCR calls)
                if self.n == 11:
                    raise DocumentError(OCR_FAILED)
                return super().recognize(data, filename=filename)

        flaky = FlakyOCR()
        spy = SpyRasterizer()
        self.intel.ocr = flaky
        self.intel.rasterizer = spy
        self.spy = spy
        data = _blank_pdf(15) + b"\x00" * 100
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="retry.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="rtry",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        wf_id = row.metadata_safe["workflow_id"]
        await self._run_to_complete(wf_id, max_ticks=120)
        self.assertEqual(self.sm.get(wf_id).status, STATUS_COMPLETED)
        # batch0 + batch1 + failed batch2 + retried batch2 = 4 raster calls
        self.assertEqual(len(spy.calls), 4)
        self.assertEqual(spy.calls[0], (1, 2, 3, 4, 5))
        self.assertEqual(spy.calls[1], (6, 7, 8, 9, 10))
        self.assertEqual(spy.calls[2], (11, 12, 13, 14, 15))
        self.assertEqual(spy.calls[3], (11, 12, 13, 14, 15))

    async def test_merge_order_and_idempotent(self):
        data = _blank_pdf(10) + b"\x00" * 100
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="order.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="ord",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        wf_id = row.metadata_safe["workflow_id"]
        await self._run_to_complete(wf_id)
        chunks = list(self.doc_store.list_chunks(row.document_id))
        ordinals = [c.ordinal for c in chunks]
        self.assertEqual(ordinals, sorted(ordinals))
        plan1 = self.intel.plan_large_extraction(
            document_id=row.document_id,
            tenant_id="tenant-a",
            page_count=10,
            size_bytes=10_000,
            enqueue=True,
            metadata={"document_type": "pdf"},
        )
        plan2 = self.intel.plan_large_extraction(
            document_id=row.document_id,
            tenant_id="tenant-a",
            page_count=10,
            size_bytes=10_000,
            enqueue=True,
            metadata={"document_type": "pdf"},
        )
        self.assertEqual(plan1["workflow_id"], plan2["workflow_id"])

    async def test_mixed_pdf_large_batches_bounded(self):
        # Build 12-page PDF: odd pages text, even blank (OCR)
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

        w = PdfWriter()
        for i in range(12):
            w.add_blank_page(width=200, height=200)
            if i % 2 == 0:
                page = w.pages[i]
                stream = DecodedStreamObject()
                content = f"BT /F1 12 Tf 50 150 Td (Page {i + 1} text) Tj ET".encode("latin-1")
                stream.set_data(content)
                stream[NameObject("/Length")] = NumberObject(len(content))
                page[NameObject("/Contents")] = stream
                font = DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }
                )
                page[NameObject("/Resources")] = DictionaryObject(
                    {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
                )
        buf = io.BytesIO()
        w.write(buf)
        data = buf.getvalue() + b"\x00" * 100
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="mixed-large.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="mixl",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        wf_id = row.metadata_safe["workflow_id"]
        await self._run_to_complete(wf_id)
        self.assertEqual(self.sm.get(wf_id).status, STATUS_COMPLETED)
        # 12 pages / 5 → 3 batches; each raster call ≤5 pages (blank pages only)
        self.assertEqual(len(self.spy.calls), 3)
        for call in self.spy.calls:
            self.assertLessEqual(len(call), 5)
            self.assertTrue(all(p % 2 == 0 for p in call))  # even page nums are blank
        locs = " ".join(c.source_location or "" for c in self.doc_store.list_chunks(row.document_id))
        for p in range(1, 13):
            self.assertIn(f"page:{p}", locs)

    def test_text_and_spreadsheet_plan_policies(self):
        from documents.intelligence.large import build_large_doc_plan

        text_plan = build_large_doc_plan(
            document_id="t1",
            tenant_id="tenant-a",
            document_type="txt",
            text_chars=250_000,
            max_text_chars_per_batch=100_000,
        )
        self.assertEqual(text_plan["strategy"], "text_char_batches")
        self.assertEqual(text_plan["batch_count"], 3)
        for b in text_plan["batches"]:
            self.assertTrue(b["bounded"])
            self.assertLessEqual(b["char_end"] - b["char_start"], 100_000)

        xls_plan = build_large_doc_plan(
            document_id="x1",
            tenant_id="tenant-a",
            document_type="xlsx",
            page_count=0,
        )
        self.assertEqual(xls_plan["strategy"], "full_document_fallback")
        self.assertEqual(xls_plan["batch_count"], 1)
        self.assertFalse(xls_plan["batches"][0]["bounded"])

    async def test_cross_tenant_denied_for_results(self):
        data = _blank_pdf(12) + b"\x00" * 100
        row = self.svc.ingest(
            DocumentIngestRequest(
                scope=_scope(tenant="tenant-a"),
                filename="deny.pdf",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="deny1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        wf_id = row.metadata_safe["workflow_id"]
        await self._run_to_complete(wf_id)
        other = MemoryScope(scope_type=SCOPE_PROJECT, scope_id="proj-blk", tenant_ref="tenant-b")
        self.assertIsNone(self.svc.get(row.document_id, requesting_scope=other))
        self.assertEqual(list(self.svc.list_chunks(row.document_id, requesting_scope=other)), [])
        found_b = self.sm.find_by_execution_key(
            row.metadata_safe["execution_key"],
            tenant_id=normalize_tenant_id("tenant-b"),
        )
        self.assertIsNone(found_b)


class Pypdfium2SmokeTests(unittest.TestCase):
    def test_real_rasterizer_if_installed(self):
        try:
            import pypdfium2  # noqa: F401
        except ImportError:
            self.skipTest("pypdfium2 not installed")
        from documents.intelligence.raster import Pypdfium2Rasterizer

        r = Pypdfium2Rasterizer()
        self.assertTrue(r.available)
        pages = r.rasterize(_blank_pdf(1), pages=(1,))
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0].image_bytes.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
