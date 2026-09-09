"""Product enrichment pipeline: generic characteristic normalization,
unit conversion, Bitrix bridging, and fact merging/conflict detection
(requirement 3)."""

from __future__ import annotations

import unittest

from product_enrichment.characteristics import (
    CANONICAL_CHARACTERISTIC_ALIASES,
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

    def test_html_markup_is_stripped_before_line_extraction(self):
        """Regression: ``scrape.fetch``'s ``body_text`` is raw, undecoded
        HTML (tools.platform.web_fetch_adapter.WebFetchAdapter never
        extracts plain text). A single-line page containing
        ``<meta property="og:image" content="...">`` must never be
        misread as a "label: value" spec line -- previously the FIRST
        colon in the whole line (inside the ``og:image`` attribute) was
        used as the split point, extracting garbage like a fragment of
        the raw markup as the "value"."""
        html = (
            '<html><head><meta property="og:image" '
            'content="https://example.com/hero.png"></head>'
            "<body>Цвет: черный</body></html>"
        )
        lines = list(extract_spec_lines(html))
        # The only real spec line (Цвет: черный) must still be found ...
        self.assertIn(("Цвет", "черный"), lines)
        # ... and nothing derived from the meta/og markup must appear.
        for label, value in lines:
            self.assertNotIn("http", value)
            self.assertNotIn("<", label)
            self.assertNotIn("<", value)

    def test_script_and_style_block_contents_are_never_treated_as_spec_lines(self):
        html = (
            "<style>body{color:red;background:blue}</style>"
            '<script>var x = {"a": "b", "resolution": "9999x9999"};</script>'
            "<body>Диагональ экрана: 139 см</body>"
        )
        lines = list(extract_spec_lines(html))
        self.assertIn(("Диагональ экрана", "139 см"), lines)
        self.assertFalse(any("9999" in value for _label, value in lines))
        self.assertFalse(any("color" in label.casefold() for label, value in lines))


class HdmiCountAliasRegressionTests(unittest.TestCase):
    """Regression: three entries in ``CANONICAL_CHARACTERISTIC_ALIASES``
    (``hdmi_count``, ``usb_count``, ``vesa_mount``) had their aliases
    written as a bare string in parentheses (e.g. ``("hdmi")``) instead of
    a one-element tuple (``("hdmi",)``). Python treats ``("hdmi")`` as the
    plain string ``"hdmi"``; iterating a string in the ``_LABEL_LOOKUP``
    comprehension yields its individual CHARACTERS ('h', 'd', 'm', 'i') as
    single-character aliases, so ``match_canonical_key`` matched these keys
    for almost any text containing any of those extremely common letters
    -- e.g. real HTML markup like "<html><head><meta ...". This is a
    correctness-critical bug for production research: once real
    manufacturer/retailer pages are actually fetched (Brave search +
    scrape.fetch), this false-matching would have silently corrupted
    characteristics with garbage values on nearly every page."""

    def test_every_alias_entry_is_a_real_tuple_not_a_bare_string(self):
        for key, (_unit, aliases) in CANONICAL_CHARACTERISTIC_ALIASES.items():
            self.assertNotIsInstance(aliases, str, f"{key!r} aliases must be a tuple, not a bare string")
            self.assertIsInstance(aliases, tuple, f"{key!r} aliases must be a tuple")

    def test_hdmi_alias_matches_the_whole_word_only(self):
        self.assertEqual(match_canonical_key("HDMI разъёмы"), "hdmi_count")

    def test_usb_alias_matches_the_whole_word_only(self):
        self.assertEqual(match_canonical_key("USB порты"), "usb_count")

    def test_vesa_alias_matches_the_whole_word_only(self):
        self.assertEqual(match_canonical_key("VESA крепление"), "vesa_mount")

    def test_unrelated_label_containing_single_alias_letters_does_not_match(self):
        for label in ("head", "meta", "image", "did", "modem"):
            self.assertNotEqual(match_canonical_key(label), "hdmi_count")
            self.assertNotEqual(match_canonical_key(label), "usb_count")
            self.assertNotEqual(match_canonical_key(label), "vesa_mount")

    def test_unrelated_label_with_no_known_alias_returns_none(self):
        self.assertIsNone(match_canonical_key("head"))
        self.assertIsNone(match_canonical_key("meta"))


if __name__ == "__main__":
    unittest.main()
