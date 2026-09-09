"""Complete-product-card follow-up pass (second Block 5.6 follow-up defect
closure -- real products 992/993 shipped as a "skeleton" product card).

Covers the orchestration-layer behavior added to
``business_assistant.controlled_bitrix_write`` and
``integrations.bitrix.schema`` for:

- item A: deterministic, fail-closed category/subcategory -> real Bitrix
  ``iblockSectionId`` resolution (never a guess, never catalog root when a
  category was actually supplied but does not resolve unambiguously);
- item C: the same fail-closed philosophy for characteristic/property
  mapping (``schema.map_characteristics_to_properties``).

``tests/test_bitrix_live_product_create_write.py`` already covers the
LOWER layer (``LiveBitrixAdapter`` field construction/validation once an
id is already resolved) in its ``SectionAssignmentTests`` /
``CharacteristicsWriteTests``. This file covers the layer ABOVE that:
resolving a Panda category/subcategory name to that id in the first place,
end to end through ``prepare_single_product_write``/
``execute_single_product_write``, including the "no root placement when a
section is required but unresolved" requirement.

Every test below runs against the mocked HTTP transport
(``BoundedHttpClient.request`` patched) already used throughout
``tests/test_bitrix_live_product_create_write.py`` -- zero real network
calls, zero real Bitrix mutations, from this file or from Cursor, at any
point. Real production product 992 / offer 993 are never touched.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from business_assistant.controlled_bitrix_write import (
    STATUS_REQUIRES_APPROVAL,
    STATUS_UNRESOLVED,
    STATUS_WRITE_VERIFIED,
    SingleProductWriteRequest,
    execute_single_product_write,
    prepare_single_product_write,
)
from integrations.bitrix import schema
from integrations.production.http import BoundedHttpClient

from tests.test_bitrix_live_product_create_write import (
    CREATED_PRODUCT_ID,
    TARGET_BRAND,
    TARGET_PURCHASE_PRICE,
    TARGET_RETAIL_PRICE,
    TARGET_SKU,
    TARGET_TENANT,
    TARGET_TITLE,
    _bridge_and_activation,
    _LiveEnv,
    _RecordingTransport,
)

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory", "iblockSectionId": 61}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika", "iblockSectionId": None}


def _request(**overrides) -> SingleProductWriteRequest:
    base = dict(
        tenant_id=TARGET_TENANT,
        title=TARGET_TITLE,
        sku=TARGET_SKU,
        retail_price=TARGET_RETAIL_PRICE,
        brand=TARGET_BRAND,
        purchase_price=TARGET_PURCHASE_PRICE,
    )
    base.update(overrides)
    return SingleProductWriteRequest(**base)


class ResolveSectionIdUnitTests(unittest.TestCase):
    """Direct, pure-function coverage of ``schema.resolve_section_id`` --
    no HTTP, no bridge, just the deterministic matching/fail-closed logic
    itself."""

    def test_exact_single_match_on_subcategory_resolves(self):
        resolved = schema.resolve_section_id(
            category="Электроника", subcategory="Телевизоры", sections=[TV_SECTION, ELECTRONICS_SECTION]
        )
        self.assertEqual(resolved["section_id"], TV_SECTION_ID)
        self.assertEqual(resolved["name"], "Телевизоры")
        self.assertEqual(resolved["matched_on"], "Телевизоры")

    def test_case_and_whitespace_insensitive_match(self):
        resolved = schema.resolve_section_id(subcategory="  ТЕЛЕВИЗОРЫ  ", sections=[TV_SECTION])
        self.assertEqual(resolved["section_id"], TV_SECTION_ID)

    def test_falls_back_to_category_when_subcategory_not_supplied(self):
        resolved = schema.resolve_section_id(category="Электроника", sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.assertEqual(resolved["section_id"], 61)

    def test_subcategory_takes_precedence_over_category_when_both_supplied(self):
        resolved = schema.resolve_section_id(
            category="Электроника", subcategory="Телевизоры", sections=[TV_SECTION, ELECTRONICS_SECTION]
        )
        self.assertEqual(resolved["section_id"], TV_SECTION_ID)

    def test_ambiguous_name_fails_closed(self):
        duplicate = dict(TV_SECTION, id=71)
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(subcategory="Телевизоры", sections=[TV_SECTION, duplicate])
        self.assertEqual(ctx.exception.code, "ambiguous_section_name")

    def test_no_matching_section_fails_closed_never_defaults_to_root(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(subcategory="Холодильники", sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.assertEqual(ctx.exception.code, "no_matching_section_found")

    def test_no_category_or_subcategory_supplied_fails_closed(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(sections=[TV_SECTION])
        self.assertEqual(ctx.exception.code, "section_name_not_supplied")

    def test_subcategory_not_found_does_not_silently_fall_back_to_category(self):
        """If a specific subcategory WAS supplied but does not match, this
        must fail closed rather than silently falling back to the broader
        category and landing in some unintended parent section."""
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(
                category="Электроника", subcategory="Холодильники", sections=[TV_SECTION, ELECTRONICS_SECTION]
            )
        self.assertEqual(ctx.exception.code, "no_matching_section_found")


class MapCharacteristicsUnitTests(unittest.TestCase):
    """Direct, pure-function coverage of
    ``schema.map_characteristics_to_properties``."""

    def test_verified_keys_map_to_their_property_fields(self):
        fields, unmapped = schema.map_characteristics_to_properties(
            {"screen_diagonal_cm": "81", "color": "Черный"}
        )
        self.assertEqual(fields, {"property154": "81", "property246": "Черный"})
        self.assertEqual(unmapped, [])

    def test_unverified_keys_are_reported_unmapped_never_written(self):
        fields, unmapped = schema.map_characteristics_to_properties(
            {"refresh_rate_hz": "120", "display_technology": "OLED"}
        )
        self.assertEqual(fields, {})
        self.assertEqual(sorted(unmapped), ["display_technology", "refresh_rate_hz"])

    def test_empty_or_none_values_are_skipped_not_reported_unmapped_or_written(self):
        fields, unmapped = schema.map_characteristics_to_properties({"screen_diagonal_cm": "", "color": None})
        self.assertEqual(fields, {})
        self.assertEqual(unmapped, [])

    def test_empty_input_is_a_no_op(self):
        fields, unmapped = schema.map_characteristics_to_properties({})
        self.assertEqual(fields, {})
        self.assertEqual(unmapped, [])

    def test_mixed_verified_and_unverified_keys_only_writes_the_verified_ones(self):
        fields, unmapped = schema.map_characteristics_to_properties(
            {"operating_system": "webOS", "model_year": "2026"}
        )
        self.assertEqual(fields, {"property206": "webOS"})
        self.assertEqual(unmapped, ["model_year"])


class SectionResolutionOrchestrationTests(unittest.TestCase):
    """End-to-end, through ``prepare_single_product_write``/
    ``execute_single_product_write`` on a LIVE bridge with a mocked
    ``catalog.section.list`` -- proves the resolved id actually reaches
    the real ``catalog.product.add`` call and the read-back expectation,
    and that an unresolved section NEVER falls through to catalog root."""

    def test_unambiguous_subcategory_resolves_and_is_written_on_create(self):
        transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(subcategory="Телевизоры"), approved=True
            )

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(result["section_id_written"], TV_SECTION_ID)
        self.assertEqual(result["resolved_section_id"], TV_SECTION_ID)

        methods_called = [m for m, _ in transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.SECTION_FIELD], TV_SECTION_ID)

        # Read-back verification was extended to also confirm the section.
        self.assertTrue(result["read_back"]["matches"])
        self.assertEqual(result["read_back"]["expected"][schema.SECTION_FIELD], TV_SECTION_ID)

    def test_category_fallback_resolves_when_no_subcategory_supplied(self):
        transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(category_source="Электроника"), approved=True
            )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(result["section_id_written"], 61)

    def test_ambiguous_section_fails_closed_with_zero_mutating_calls(self):
        duplicate_tv = dict(TV_SECTION, id=71)
        transport = _RecordingTransport(sections=[TV_SECTION, duplicate_tv])
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            preview = prepare_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(subcategory="Телевизоры")
            )
        self.assertEqual(preview["status"], STATUS_UNRESOLVED)
        self.assertEqual(preview["reason"], "ambiguous_section_name")
        # Only the read-only section lookup happened -- never a create.
        methods_called = [m for m, _ in transport.calls]
        self.assertEqual(methods_called, ["catalog.section.list"])
        self.assertNotIn("catalog.product.add", methods_called)

    def test_no_matching_section_fails_closed_instead_of_landing_at_catalog_root(self):
        transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(subcategory="Холодильники"), approved=True
            )
        self.assertEqual(result["status"], STATUS_UNRESOLVED)
        self.assertEqual(result["reason"], "no_matching_section_found")
        self.assertFalse(result["mutated"])  # preview short-circuit, never claims a create happened

        methods_called = [m for m, _ in transport.calls]
        self.assertNotIn("catalog.product.add", methods_called)

    def test_section_lookup_transport_failure_fails_closed_not_verified(self):
        def _boom(method, url, **kwargs):
            raise ConnectionError("simulated network outage")

        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_boom):
            bridge, _ = _bridge_and_activation()
            preview = prepare_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(subcategory="Телевизоры")
            )
        self.assertEqual(preview["status"], STATUS_UNRESOLVED)
        self.assertEqual(preview["reason"], "section_lookup_failed")

    def test_no_category_or_subcategory_supplied_never_triggers_section_lookup(self):
        """Baseline: a request that supplies no category/subcategory at
        all must behave exactly as before -- no section lookup HTTP call,
        no section field on the create, full success otherwise."""
        transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertIsNone(result["section_id_written"])
        methods_called = [m for m, _ in transport.calls]
        self.assertNotIn("catalog.section.list", methods_called)


class CharacteristicsOrchestrationTests(unittest.TestCase):
    """End-to-end characteristic mapping through the full write path on a
    LIVE bridge -- proves verified characteristics reach
    ``catalog.product.add`` and unverified ones are reported
    sourced-but-unwritten, never guessed onto a property."""

    def test_verified_characteristics_are_written_and_reported(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge,
                tenant_id=TARGET_TENANT,
                request=_request(characteristics={"screen_diagonal_cm": "81", "color": "Черный"}),
                approved=True,
            )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(sorted(result["characteristics_written"]), ["color", "screen_diagonal_cm"])
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"]["property154"], "81")
        self.assertEqual(product_body["fields"]["property246"], "Черный")

    def test_unverified_characteristics_are_reported_not_written_on_live_bridge(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge,
                tenant_id=TARGET_TENANT,
                request=_request(characteristics={"refresh_rate_hz": "120"}),
                approved=True,
            )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(result["characteristics_written"], [])
        not_written_fields = {item["field"] for item in result["not_written"]}
        self.assertIn("characteristic:refresh_rate_hz", not_written_fields)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        # property100 (BRAND) is expected -- the request always supplies a
        # brand; only the characteristic property fields must be absent.
        self.assertNotIn(f"property{schema.CATALOG_CHARACTERISTICS[0].property_id}", product_body["fields"])
        characteristic_property_keys = {f"property{c.property_id}" for c in schema.CATALOG_CHARACTERISTICS}
        self.assertTrue(characteristic_property_keys.isdisjoint(product_body["fields"]))

    def test_no_characteristics_supplied_is_unchanged_baseline_behavior(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request())
        self.assertEqual(preview["status"], STATUS_REQUIRES_APPROVAL)
        self.assertNotIn("characteristics", preview["canonical_payload"])


class PhysicalAndContentOrchestrationTests(unittest.TestCase):
    """End-to-end weight/dimensions + preview/detail content through the
    full write path -- proves the canonical payload built by
    ``controlled_bitrix_write`` actually reaches the real
    ``catalog.product.add`` call, and that malformed physical data fails
    closed before any HTTP call at all (item D / E)."""

    def test_weight_and_dimensions_reach_the_real_create_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge,
                tenant_id=TARGET_TENANT,
                request=_request(weight_g="12000", length_mm="720", width_mm="420", height_mm="60"),
                approved=True,
            )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.WEIGHT_FIELD], "12000")
        self.assertEqual(product_body["fields"][schema.LENGTH_FIELD], "720")
        self.assertEqual(product_body["fields"][schema.WIDTH_FIELD], "420")
        self.assertEqual(product_body["fields"][schema.HEIGHT_FIELD], "60")

    def test_missing_dimensions_are_never_forced_to_zero(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(weight_g="12000"), approved=True
            )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.WEIGHT_FIELD], "12000")
        for key in (schema.LENGTH_FIELD, schema.WIDTH_FIELD, schema.HEIGHT_FIELD):
            self.assertNotIn(key, product_body["fields"])

    def test_malformed_dimension_fails_closed_before_any_http_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            preview = prepare_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(weight_g="not-a-number")
            )
        self.assertEqual(preview["status"], STATUS_UNRESOLVED)
        self.assertEqual(preview["reason"], "invalid_weight_g")
        self.assertEqual(transport.calls, [])

    def test_short_and_detailed_descriptions_reach_the_real_create_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge,
                tenant_id=TARGET_TENANT,
                request=_request(short_description="Короткое", detailed_description="Подробное"),
                approved=True,
            )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.PREVIEW_TEXT_FIELD], "Короткое")
        self.assertEqual(product_body["fields"][schema.DETAIL_TEXT_FIELD], "Подробное")


if __name__ == "__main__":
    unittest.main()
