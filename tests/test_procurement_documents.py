"""Unit tests for document-backed offer normalization."""

from __future__ import annotations

import io
import unittest
from decimal import Decimal

from documents.models import SOURCE_TEST_FIXTURE, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from openpyxl import Workbook
from procurement.models import OfferProvenance, content_hash_text
from procurement.normalizer import OfferNormalizer
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class ProcurementDocumentsTests(unittest.TestCase):
    def test_offer_from_document_row(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["unit_price", "quantity", "currency", "color"])
        ws.append([10, 5, "USD", "blue"])
        buf = io.BytesIO()
        wb.save(buf)
        scope = _scope()
        doc_svc = DocumentService(InMemoryDocumentStore())
        row = doc_svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="offers.xlsx",
                content=buf.getvalue(),
                source_type=SOURCE_TEST_FIXTURE,
                source_id="xlsx-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertIsNotNone(row.document_id)
        parsed = doc_svc._parsed_cache[row.document_id]
        cells = parsed.cells
        data_row = {
            "unit_price": 10,
            "quantity": 5,
            "currency": "USD",
            "specifications": {"color": "blue"},
        }
        prov = OfferProvenance(
            source_id="doc",
            source_ref=row.document_id,
            retrieved_at=utc_now(),
            content_hash=content_hash_text(row.document_id),
            trust="document_sourced",
            document_id=row.document_id,
        )
        offer = OfferNormalizer().from_document_row(
            offer_id="o1",
            request_id="r1",
            supplier_id="s1",
            scope=scope,
            row=data_row,
            provenance=prov,
            source_ref=row.document_id,
        )
        self.assertEqual(offer.unit_price.amount, Decimal("10"))
        self.assertEqual(offer.quantity, Decimal("5"))
        self.assertEqual(offer.currency, "USD")
        self.assertTrue(cells)


if __name__ == "__main__":
    unittest.main()
