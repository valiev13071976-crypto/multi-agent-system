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
from workflow.models import STATUS_COMPLETED
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
