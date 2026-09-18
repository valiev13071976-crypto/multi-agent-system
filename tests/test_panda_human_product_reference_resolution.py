"""Human product reference resolution -- generic, data-driven acceptance
tests for ``data_intel.service._resolve_product_reference``.

Production requirement (product-first end-to-end task, post PR #97): a
normal user must be able to select a product WITHOUT copying its exact
machine identifier -- normalized spacing/separators, or a brand +
meaningful partial model/article, must resolve uniquely; a genuinely
ambiguous or unknown reference must never be guessed.

Multiple STRUCTURALLY DIFFERENT fixture products are used throughout so
no single literal SKU/brand/model becomes part of the production
contract -- these are FIXTURE DATA ONLY.
"""

from __future__ import annotations

import io
import unittest

TENANT = "tenant-human-reference"


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["article", "product_name", "brand", "ean", "purchase_price"])
    # Canonical article uses dots/hyphens -- a human will often type it
    # with plain spaces instead (item C).
    ws.append(["ABC32LQ-63806.LC", "Телевизор Дельта", "LG", "4600000000201", "10000.00"])
    # A second, unrelated product sharing NEITHER article shape nor name
    # substring, so it never accidentally becomes a false-positive match.
    ws.append(["XYZ-55UP-9100.KT", "Телевизор Омега", "Samsung", "4600000000202", "22000.00"])
    # A third product with a similar-looking but genuinely different
    # article, used for the ambiguity test (shares a long common
    # normalized substring AND the same brand as row 1).
    ws.append(["ABC32LQ-63807.LC", "Телевизор Дельта Плюс", "LG", "4600000000203", "10500.00"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class HumanProductReferenceResolutionTests(unittest.TestCase):
    def setUp(self):
        from data_intel.service import DataIntelligenceService
        from data_intel.store import InMemoryDatasetStore

        self.svc = DataIntelligenceService(InMemoryDatasetStore())
        ingested = self.svc.ingest(_xlsx_bytes(), filename="ref.xlsx", tenant_id=TENANT)
        self.dataset_id = ingested["dataset_id"]
        desc = self.svc.store.get_dataset(self.dataset_id, tenant_id=TENANT)
        self.table = desc.tables[0]
        self.rows = self.svc.store.get_rows(self.dataset_id, tenant_id=TENANT)

    def _resolve(self, text):
        from data_intel.service import _resolve_product_reference

        return _resolve_product_reference(text, self.rows, self.table)

    def test_a_exact_article_selects_correct_product(self):
        status, payload = self._resolve("покажи товар XYZ-55UP-9100.KT")
        self.assertEqual(status, "UNIQUE")
        row_index, _col, _value = payload
        self.assertEqual(self.rows[row_index]["article"], "XYZ-55UP-9100.KT")

    def test_b_exact_ean_selects_correct_product(self):
        status, payload = self._resolve("EAN 4600000000202, что по нему известно?")
        self.assertEqual(status, "UNIQUE")
        row_index, _col, _value = payload
        self.assertEqual(self.rows[row_index]["ean"], "4600000000202")

    def test_c_normalized_separators_select_same_unique_product(self):
        # Same identity as row 0's article, but typed with plain spaces
        # instead of the source file's own dots/hyphens.
        status, payload = self._resolve("покажи товар XYZ 55UP 9100 KT пожалуйста")
        self.assertEqual(status, "UNIQUE")
        row_index, _col, _value = payload
        self.assertEqual(self.rows[row_index]["article"], "XYZ-55UP-9100.KT")

    def test_d_brand_plus_meaningful_partial_model_selects_unique_candidate(self):
        # Row 0 and row 2 share the SAME brand and a common article
        # prefix ("ABC32LQ") -- but this fragment also names the part
        # that actually DIFFERS between them ("63806" vs "63807"), so it
        # uniquely narrows to row 0 even though it is only a PARTIAL
        # (missing the leading "ABC" and the trailing "." separator)
        # human rendering of the canonical article.
        status, payload = self._resolve("покажи товар LG 63806LC")
        self.assertEqual(status, "UNIQUE")
        row_index, _col, _value = payload
        self.assertEqual(self.rows[row_index]["article"], "ABC32LQ-63806.LC")

    def test_e_ambiguous_partial_model_does_not_guess(self):
        # "ABC32LQ" + brand "LG" alone plausibly matches BOTH row 0 and
        # row 2 (both LG, both share the "ABC32LQ" prefix) -- must not
        # guess, must return real candidates.
        status, payload = self._resolve("покажи товар LG ABC32LQ")
        self.assertEqual(status, "AMBIGUOUS")
        self.assertEqual(len(payload), 2)
        self.assertTrue(all(isinstance(c, str) and c for c in payload))

    def test_f_unknown_reference_does_not_guess(self):
        status, payload = self._resolve("покажи товар совершенно неизвестный код QQQ999")
        self.assertEqual(status, "NONE")
        self.assertIsNone(payload)

    def test_bare_brand_alone_never_matches(self):
        """A bare brand mention with no model/article fragment at all must
        never resolve by itself (two LG rows exist) -- covers the "brand
        alone is not enough" safety rule."""
        status, payload = self._resolve("покажи товар LG")
        self.assertIn(status, ("AMBIGUOUS", "NONE"))
        if status == "UNIQUE":  # pragma: no cover -- defensive, must never happen
            self.fail("bare brand mention must never uniquely resolve a product")

    def test_empty_text_is_no_match(self):
        status, payload = self._resolve("   ")
        self.assertEqual(status, "NONE")
        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
