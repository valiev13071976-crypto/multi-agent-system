"""Production blocker: ``no existing Bitrix section matched 'CE'``.

The supplier price list this installation uploads names its category
column with an internal code -- the real captured row is
``category="CE"``, ``product_name="Телевизор LG 32LQ63006LA.ARUG"`` (see
``tests/test_bitrix_live_product_create_write.py``). "CE" names no
section in any catalog, so treating the spreadsheet's category as the
authoritative section name can never resolve, no matter how good the
name matching is.

The prepared card, however, already says what the product IS: its title
and the canonical characteristic keys enrichment resolved. Those signals
now act as evidence when (and only when) the supplier category matches no
section, and are resolved against the REAL sections from
``catalog.section.list`` -- the same verbatim production snapshot
captured in ``tests/fixtures/bitrix_catalog_section_list_production.json``.

Every Bitrix call below is answered by a mocked transport: zero live
mutations.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from business_assistant.controlled_bitrix_write import (
    STATUS_UNRESOLVED,
    STATUS_WRITE_VERIFIED,
    execute_single_product_write,
)
from integrations.bitrix import schema
from integrations.production.http import BoundedHttpClient
from tests.test_bitrix_live_product_create_write import (
    TARGET_TENANT,
    _bridge_and_activation,
    _LiveEnv,
    _RecordingTransport,
    _request,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bitrix_catalog_section_list_production.json"

# The real supplier row shape captured during the LIVE audit: a broad
# internal category code plus a product name that states the type.
SUPPLIER_CATEGORY_CODE = "CE"
TV_PRODUCT_TITLE = "Телевизор LG 32LQ63006LA.ARUG"


def _production_sections() -> list:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        doc = json.load(handle)
    return [s for page in doc["pages"] for s in page["response"]["result"]["sections"]]


def _section_id_named(name: str) -> int:
    return next(s["id"] for s in _production_sections() if s["name"] == name)


class DerivedProductCategoryResolutionTests(unittest.TestCase):
    def setUp(self):
        self.sections = _production_sections()

    def test_broad_supplier_code_resolves_from_the_product_title(self):
        resolved = schema.resolve_section_id(
            category=SUPPLIER_CATEGORY_CODE,
            sections=self.sections,
            signals=(TV_PRODUCT_TITLE, "screen_diagonal_cm", "color"),
        )
        self.assertEqual(resolved["section_id"], _section_id_named("Телевизоры"))
        self.assertEqual(resolved["match_kind"], "product_evidence_concept")

    def test_broad_supplier_code_resolves_from_a_type_specific_characteristic(self):
        """No type word in the title at all -- the canonical
        ``smart_tv_support`` key only exists for a television."""
        resolved = schema.resolve_section_id(
            category=SUPPLIER_CATEGORY_CODE,
            sections=self.sections,
            signals=("LG 55MRGB86B6A.ARUG", "smart_tv_support", "screen_diagonal_cm"),
        )
        self.assertEqual(resolved["section_id"], _section_id_named("Телевизоры"))

    def test_same_mechanism_resolves_a_different_product_type(self):
        for title, expected_section in (
            ("Смартфон Samsung Galaxy S24 256GB", "Смартфоны"),
            ("Наушники Sony WH-1000XM5", "Наушники"),
            ("Смарт-часы Apple Watch SE", "Смарт-часы"),
        ):
            with self.subTest(title=title):
                resolved = schema.resolve_section_id(
                    category="Consumer Electronics", sections=self.sections, signals=(title,)
                )
                self.assertEqual(resolved["section_id"], _section_id_named(expected_section))

    def test_precise_supplier_category_is_unchanged_by_product_evidence(self):
        resolved = schema.resolve_section_id(
            category="Наушники", sections=self.sections, signals=("Наушники Sony WH-1000XM5",)
        )
        self.assertEqual(resolved["section_id"], _section_id_named("Наушники"))
        self.assertEqual(resolved["match_kind"], "exact_name")

    def test_supplier_category_that_does_match_a_section_still_wins(self):
        """Evidence is a FALLBACK, never an override: a supplier category
        that resolves on its own keeps resolving to exactly what it named,
        even when the product evidence points somewhere more specific."""
        resolved = schema.resolve_section_id(
            category="Электроника", sections=self.sections, signals=(TV_PRODUCT_TITLE,)
        )
        self.assertEqual(resolved["section_id"], _section_id_named("Электроника"))

    def test_supplier_naming_a_category_this_catalog_lacks_still_fails_closed(self):
        """"Холодильники" is a real product category, just not one this
        installation sells. The supplier and the product disagree, so the
        write fails closed instead of being silently rerouted to the TV
        section by the title."""
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(
                category="Электроника",
                subcategory="Холодильники",
                sections=self.sections,
                signals=(TV_PRODUCT_TITLE, "smart_tv_support"),
            )
        self.assertEqual(ctx.exception.code, "no_matching_section_found")

    def test_unknown_product_type_still_fails_closed(self):
        for title in ("Гироскутер Ninebot S2", "Холодильник LG GA-B509", "LG 55MRGB86B6A.ARUG"):
            with self.subTest(title=title):
                with self.assertRaises(schema.SectionResolutionError) as ctx:
                    schema.resolve_section_id(
                        category=SUPPLIER_CATEGORY_CODE, sections=self.sections, signals=(title,)
                    )
                self.assertEqual(ctx.exception.code, "no_matching_section_found")

    def test_evidence_pointing_at_unrelated_branches_fails_closed(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(
                category=SUPPLIER_CATEGORY_CODE,
                sections=self.sections,
                signals=("Комплект: телевизор LG и наушники Sony",),
            )
        self.assertEqual(ctx.exception.code, "ambiguous_section_name")

    def test_duplicate_section_names_still_fail_closed(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(
                category="Аксессуары", sections=self.sections, signals=("Кабель HDMI",)
            )
        self.assertEqual(ctx.exception.code, "ambiguous_section_name")

    def test_no_signals_behaves_exactly_as_before(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(category=SUPPLIER_CATEGORY_CODE, sections=self.sections)
        self.assertEqual(ctx.exception.code, "no_matching_section_found")


class GovernedWritePassesPreparedSignalsTests(unittest.TestCase):
    """The write path itself must hand those already-prepared signals to
    the resolver -- otherwise the production row still fails closed."""

    def test_ce_row_with_a_television_title_writes_into_the_real_tv_section(self):
        transport = _RecordingTransport(sections=_production_sections())
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge,
                tenant_id=TARGET_TENANT,
                request=_request(
                    title=TV_PRODUCT_TITLE,
                    category_source=SUPPLIER_CATEGORY_CODE,
                    characteristics={"screen_diagonal_cm": "80", "color": "черный"},
                ),
                approved=True,
            )

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        expected_section_id = _section_id_named("Телевизоры")
        self.assertEqual(result["section_id_written"], expected_section_id)
        product_body = next(body for method, body in transport.calls if method == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.SECTION_FIELD], expected_section_id)

    def test_ce_row_without_any_product_type_evidence_never_guesses_a_section(self):
        transport = _RecordingTransport(sections=_production_sections())
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge,
                tenant_id=TARGET_TENANT,
                request=_request(title="LG 32LQ63006LA.ARUG", category_source=SUPPLIER_CATEGORY_CODE),
                approved=True,
            )

        self.assertEqual(result["status"], STATUS_UNRESOLVED)
        self.assertEqual(result["reason"], "no_matching_section_found")
        self.assertNotIn("catalog.product.add", [m for m, _ in transport.calls])


if __name__ == "__main__":
    unittest.main()
