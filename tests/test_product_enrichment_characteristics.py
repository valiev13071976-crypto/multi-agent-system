"""Product enrichment pipeline: generic characteristic normalization,
unit conversion, Bitrix bridging, and fact merging/conflict detection
(requirement 3)."""

from __future__ import annotations

import unittest

from product_enrichment.characteristics import (
    bridge_characteristics_to_bitrix,
    extract_spec_lines,
    match_canonical_key,
    merge_facts_into_characteristics,
    normalize_characteristic_value,
)
from product_enrichment.models import (
    CONFIDENCE_PROBABLE,
    CONFIDENCE_VERIFIED,
    NormalizedCharacteristic,
    SOURCE_AUTHORIZED_DISTRIBUTOR,
    SOURCE_MANUFACTURER,
    SOURCE_RETAIL_CATALOG,
    SourceFact,
)


def _fact(key, value, *, source_type=SOURCE_RETAIL_CATALOG, domain="example.com", unit="") -> SourceFact:
    return SourceFact(
        characteristic_key=key,
        raw_label=key,
        raw_value=value,
        normalized_value=value,
        unit=unit,
        source_url=f"https://{domain}/p",
        source_type=source_type,
        source_domain=domain,
        confidence=CONFIDENCE_PROBABLE,
    )


class MatchCanonicalKeyTests(unittest.TestCase):
    def test_russian_label_matches(self):
        self.assertEqual(match_canonical_key("Диагональ экрана"), "screen_diagonal_cm")

    def test_english_label_matches(self):
        self.assertEqual(match_canonical_key("Screen Resolution"), "screen_resolution")

    def test_unknown_label_returns_none(self):
        self.assertIsNone(match_canonical_key("совершенно неизвестная характеристика"))

    def test_longest_alias_wins_over_shorter_shadowing_one(self):
        # "разрешение экрана" and a shorter alias must not clash/misresolve.
        self.assertEqual(match_canonical_key("разрешение экрана"), "screen_resolution")


class NormalizeCharacteristicValueTests(unittest.TestCase):
    def test_inches_converted_to_cm_for_screen_diagonal(self):
        value, unit = normalize_characteristic_value("screen_diagonal_cm", '55"')
        self.assertEqual(unit, "cm")
        self.assertAlmostEqual(float(value), 55 * 2.54, places=1)

    def test_cm_value_passed_through(self):
        value, unit = normalize_characteristic_value("screen_diagonal_cm", "139 см")
        self.assertEqual(value, "139")
        self.assertEqual(unit, "cm")

    def test_bare_number_assumed_already_cm_never_reinterpreted_as_inches(self):
        value, unit = normalize_characteristic_value("screen_diagonal_cm", "139")
        self.assertEqual(value, "139")
        self.assertEqual(unit, "cm")

    def test_other_keys_pass_through_verbatim_with_declared_unit(self):
        value, unit = normalize_characteristic_value("operating_system", "webOS 24")
        self.assertEqual(value, "webOS 24")
        self.assertEqual(unit, "")


class BridgeCharacteristicsToBitrixTests(unittest.TestCase):
    def test_verified_key_gets_bitrix_property_id(self):
        chars = {
            "screen_diagonal_cm": NormalizedCharacteristic(
                key="screen_diagonal_cm", value="139", unit="cm", confidence=CONFIDENCE_VERIFIED
            )
        }
        bridged = bridge_characteristics_to_bitrix(chars)
        self.assertEqual(bridged["screen_diagonal_cm"].bitrix_property_id, 154)
        self.assertTrue(bridged["screen_diagonal_cm"].bitrix_writable)

    def test_unknown_key_kept_with_no_bitrix_property(self):
        chars = {
            "refresh_rate_hz": NormalizedCharacteristic(
                key="refresh_rate_hz", value="120", unit="Hz", confidence=CONFIDENCE_VERIFIED
            )
        }
        bridged = bridge_characteristics_to_bitrix(chars)
        self.assertIsNone(bridged["refresh_rate_hz"].bitrix_property_id)
        self.assertFalse(bridged["refresh_rate_hz"].bitrix_writable)
        # Never dropped -- the source data survives in canonical enrichment data.
        self.assertEqual(bridged["refresh_rate_hz"].value, "120")

    def test_unverified_confidence_characteristic_not_writable_even_if_bound(self):
        from product_enrichment.models import CONFIDENCE_UNVERIFIED

        chars = {
            "color": NormalizedCharacteristic(key="color", value="black", confidence=CONFIDENCE_UNVERIFIED)
        }
        bridged = bridge_characteristics_to_bitrix(chars)
        self.assertEqual(bridged["color"].bitrix_property_id, 246)
        self.assertFalse(bridged["color"].bitrix_writable)


class MergeFactsIntoCharacteristicsTests(unittest.TestCase):
    def test_single_source_yields_probable_confidence(self):
        facts = [_fact("color", "black", source_type=SOURCE_RETAIL_CATALOG, domain="retailer.example")]
        resolved, conflicts = merge_facts_into_characteristics(facts)
        self.assertEqual(resolved["color"].value, "black")
        self.assertEqual(resolved["color"].confidence, CONFIDENCE_PROBABLE)
        self.assertEqual(conflicts, ())

    def test_manufacturer_source_alone_yields_verified_confidence(self):
        facts = [_fact("color", "black", source_type=SOURCE_MANUFACTURER, domain="lg.com")]
        resolved, _ = merge_facts_into_characteristics(facts)
        self.assertEqual(resolved["color"].confidence, CONFIDENCE_VERIFIED)

    def test_two_independent_domains_agreeing_yields_verified(self):
        facts = [
            _fact("color", "black", source_type=SOURCE_AUTHORIZED_DISTRIBUTOR, domain="citilink.ru"),
            _fact("color", "black", source_type=SOURCE_RETAIL_CATALOG, domain="dns-shop.ru"),
        ]
        resolved, conflicts = merge_facts_into_characteristics(facts)
        self.assertEqual(resolved["color"].confidence, CONFIDENCE_VERIFIED)
        self.assertEqual(conflicts, ())

    def test_manufacturer_strictly_dominates_conflicting_retail_value(self):
        facts = [
            _fact("color", "black", source_type=SOURCE_MANUFACTURER, domain="lg.com"),
            _fact("color", "white", source_type=SOURCE_RETAIL_CATALOG, domain="somerandomshop.example"),
        ]
        resolved, conflicts = merge_facts_into_characteristics(facts)
        self.assertEqual(resolved["color"].value, "black")
        self.assertEqual(conflicts, ())

    def test_equally_trusted_conflicting_values_reported_as_conflict_not_guessed(self):
        facts = [
            _fact("color", "black", source_type=SOURCE_RETAIL_CATALOG, domain="shop-a.example"),
            _fact("color", "white", source_type=SOURCE_RETAIL_CATALOG, domain="shop-b.example"),
        ]
        resolved, conflicts = merge_facts_into_characteristics(facts)
        self.assertNotIn("color", resolved)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].code, "characteristic_conflict_color")

    def test_facts_without_characteristic_key_are_ignored(self):
        identity_fact = SourceFact(
            characteristic_key="",
            raw_label="model",
            raw_value="X",
            normalized_value="X",
            unit="",
            source_url="https://lg.com/x",
            source_type=SOURCE_MANUFACTURER,
            source_domain="lg.com",
            confidence=CONFIDENCE_PROBABLE,
        )
        resolved, conflicts = merge_facts_into_characteristics([identity_fact])
        self.assertEqual(resolved, {})
        self.assertEqual(conflicts, ())


class ExtractSpecLinesTests(unittest.TestCase):
    def test_extracts_colon_separated_lines(self):
        text = "Диагональ экрана: 139 см\nЦвет: черный\nrandom line with no separator"
        lines = list(extract_spec_lines(text))
        self.assertIn(("Диагональ экрана", "139 см"), lines)
        self.assertIn(("Цвет", "черный"), lines)

    def test_extracts_dash_separated_lines(self):
        text = "Operating system \u2014 webOS 24"
        lines = list(extract_spec_lines(text))
        self.assertIn(("Operating system", "webOS 24"), lines)

    def test_overlong_lines_are_skipped(self):
        text = "label: " + ("x" * 300)
        self.assertEqual(list(extract_spec_lines(text)), [])


if __name__ == "__main__":
    unittest.main()
