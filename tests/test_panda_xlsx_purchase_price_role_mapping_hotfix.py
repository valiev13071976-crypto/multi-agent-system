"""PANDA — PRODUCTION HOTFIX: purchase-price column role mapping only.

After PR #37/#38/#39/#40 the product-preview card correctly renders every
role it receives, but the production workbook's real purchase-price header

    "Предоплата,\nЦена с НДС"

(a newline-separated two-line header meaning "prepayment, price incl. VAT")
never mapped to ``ROLE_PURCHASE_PRICE``, so the card omitted purchase price
even though the value (22513.70) was sitting right there in the sheet.

Root cause: ``data_intel.mapping.map_header_role`` normalizes the header to
a single underscore-joined token string ("предоплата_цена_с_ндс") and then,
because it is not an *exact* alias key, falls through to a substring scan
over ``_ALIAS`` in dict-insertion order. The generic alias "цена" ->
ROLE_PRICE is inserted *before* any purchase-price alias and is a substring
of the normalized header, so the substring scan returns ROLE_PRICE first
and never reaches a purchase-price alias at all (none of the existing
purchase aliases, e.g. "закупка"/"cost", are substrings of this header
either -- "предоплата" was not a recognized procurement signal).

Fix (``data_intel/mapping.py`` only): before the substring scan, tokenize
the normalized header on "_" and check for the *co-occurrence* of a
price-like token ("цена"/"price"/"стоимость") with a supplier/procurement
context token ("закупка"/"закупочная"/"предоплата"/"поставщик"/"cost"/
"purchase"/...). Only when BOTH kinds of token are present does the header
map to ROLE_PURCHASE_PRICE. A bare price token alone (e.g. plain "Цена с
НДС" with no procurement context) is deliberately left unclassified as
purchase price -- it keeps resolving to ROLE_PRICE, exactly as before --
so ambiguous generic price columns are never misclassified.

No schema-inference redesign, no workbook-specific special-casing, no
change to XLSX parsing, row lookup, follow-up reuse, or preview rendering
(PR #40) -- this only feeds a previously-missing role into the same,
unmodified rendering path.
"""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from data_intel.contracts import ROLE_PRICE, ROLE_PURCHASE_PRICE, ROLE_SELLING_PRICE
from data_intel.mapping import map_header_role
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

TARGET_SKU = "32LQ63006LA.ARUG"
TARGET_PURCHASE_PRICE = "22513.70"
USER_RETAIL_PRICE_RUB = "29990"

# The real production header -- a two-line, comma-separated supplier
# purchase-price column. Deliberately not asserted against any workbook
# identity, exact row, or the numeric value's origin beyond "read from XLSX".
REAL_HEADER = "Предоплата,\nЦена с НДС"


class PurchasePriceHeaderRoleMappingTests(unittest.TestCase):
    """Unit coverage for ``map_header_role`` itself."""

    def test_real_header_with_newline_maps_to_purchase_price(self):
        role, confidence = map_header_role(REAL_HEADER)
        self.assertEqual(role, ROLE_PURCHASE_PRICE)
        self.assertNotEqual(confidence, "unresolved")

    def test_equivalent_whitespace_and_punctuation_variants_all_map(self):
        variants = [
            "Предоплата,\nЦена с НДС",
            "Предоплата,   Цена   с   НДС",
            "предоплата, цена с ндс",
            "ПРЕДОПЛАТА ЦЕНА С НДС",
            "Предоплата;Цена с НДС",
        ]
        for header in variants:
            role, _ = map_header_role(header)
            self.assertEqual(
                role, ROLE_PURCHASE_PRICE, msg=f"failed for variant: {header!r}"
            )

    def test_ambiguous_generic_price_header_stays_generic_price(self):
        # No supplier/procurement signal present -- must NOT be forced to
        # purchase price.
        role, _ = map_header_role("Цена с НДС")
        self.assertEqual(role, ROLE_PRICE)
        self.assertNotEqual(role, ROLE_PURCHASE_PRICE)

    def test_existing_selling_price_alias_still_resolves(self):
        role, confidence = map_header_role("selling_price")
        self.assertEqual(role, ROLE_SELLING_PRICE)
        self.assertEqual(confidence, "exact")

    def test_existing_purchase_price_alias_still_resolves(self):
        role, confidence = map_header_role("purchase_price")
        self.assertEqual(role, ROLE_PURCHASE_PRICE)
        self.assertEqual(confidence, "exact")


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ProductPreviewGainsPurchasePriceTests(unittest.TestCase):
    """End-to-end: the production-shaped header now surfaces in the card."""

    def _service(self):
        artifact_service = ArtifactService(store=InMemoryArtifactStore())
        svc = DataIntelligenceService(InMemoryDatasetStore())
        svc.artifact_service = artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=svc)
        ToolGateway(registry=registry, register_search=False)
        return svc

    def test_preview_includes_real_purchase_price_from_two_line_header(self):
        svc = self._service()
        content = _xlsx_bytes(
            [
                ["sku", "product_name", "ean", "category", "brand", REAL_HEADER],
                ["SAM-A54", "Galaxy A54", "1111111111111", "Phones", "Samsung", "18000.00"],
                [
                    TARGET_SKU,
                    f"Телевизор LG {TARGET_SKU}",
                    "8806096259955",
                    "CE",
                    "LG",
                    TARGET_PURCHASE_PRICE,
                ],
            ]
        )
        ing = svc.ingest(content, filename="LG_TV.xlsx", tenant_id="tenant-a", enqueue_large=False)
        result = svc.execute_nl_request(
            ing["dataset_id"],
            f"Найди товар {TARGET_SKU}, розничная цена {USER_RETAIL_PRICE_RUB} \u20bd, покажи карточку",
            tenant_id="tenant-a",
        )
        text = result.get("summary_text", "")
        self.assertIn(TARGET_PURCHASE_PRICE, text)
        self.assertIn("Закупочная цена", text)
        # Retail price supplied by the user in the same request is preserved.
        self.assertIn(USER_RETAIL_PRICE_RUB, text)
        self.assertIn("не выполнена", text)


if __name__ == "__main__":
    unittest.main()
