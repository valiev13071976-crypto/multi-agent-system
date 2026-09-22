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


class GenericCharacteristicValueQualityGateTests(unittest.TestCase):
    def test_enum_like_fields_reject_marketing_prose(self):
        garbage_values = (
            "Premium display technology brings incredible brightness and vivid cinematic colors for every scene.",
            "Experience next generation Mini LED performance with stunning contrast and exceptional brightness",
            "This smart platform gives you access to thousands of apps and entertainment services",
        )
        for key in ("panel_technology", "backlight_technology", "operating_system"):
            for value in garbage_values:
                with self.subTest(key=key, value=value):
                    normalized, _unit = normalize_characteristic_value(key, value)
                    self.assertEqual(normalized, "")

    def test_compact_real_spec_tokens_survive(self):
        expected = {
            "panel_technology": "QD-Mini LED",
            "backlight_technology": "Mini LED",
            "operating_system": "Google TV",
            "hdr_formats": "HDR10+, Dolby Vision, HLG",
            "country_of_origin": "Turkey",
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                normalized, _unit = normalize_characteristic_value(key, value)
                self.assertEqual(normalized, value)

    def test_dimensions_require_numeric_triplet_not_neighbor_label_text(self):
        self.assertEqual(
            normalize_characteristic_value(
                "dimensions_without_stand", "Carton Dimensions (LxWxH mm) 1360 x 128 x 875"
            )[0],
            "",
        )
        self.assertEqual(
            normalize_characteristic_value("dimensions_without_stand", "1224 x 711 x 69 mm")[0],
            "1224 x 711 x 69 mm",
        )

    def test_package_dimension_label_variants_are_recognized_structurally(self):
        html = (
            "<div>Dimensions without stand</div><div>1224 x 711 x 69 mm</div>"
            "<div>Carton Dimensions</div><div>1360 x 128 x 875 mm</div>"
        )
        pairs = list(extract_spec_lines(html))
        self.assertIn(("Dimensions without stand", "1224 x 711 x 69 mm"), pairs)
        self.assertIn(("Carton Dimensions", "1360 x 128 x 875 mm"), pairs)

    def test_resolution_and_vesa_reject_prose_but_keep_valid_shapes(self):
        self.assertEqual(normalize_characteristic_value("screen_resolution", "3840x2160")[0], "3840x2160")
        self.assertEqual(normalize_characteristic_value("screen_resolution", "Ultra detailed picture for movies")[0], "")
        self.assertEqual(normalize_characteristic_value("vesa_mount", "300 x 300")[0], "300 x 300")
        self.assertEqual(normalize_characteristic_value("vesa_mount", "Mounting compatible with most wall brackets")[0], "")

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
        # SIMPLE_PRODUCT TV contract-alignment pass (ticket T-F79758
        # follow-up): "refresh_rate_hz" is now a VERIFIED key (property
        # 147) and can no longer serve as an "unknown key" example here --
        # "model_year" remains genuinely unmapped.
        chars = {
            "model_year": NormalizedCharacteristic(
                key="model_year", value="2026", unit="", confidence=CONFIDENCE_VERIFIED
            )
        }
        bridged = bridge_characteristics_to_bitrix(chars)
        self.assertIsNone(bridged["model_year"].bitrix_property_id)
        self.assertFalse(bridged["model_year"].bitrix_writable)
        # Never dropped -- the source data survives in canonical enrichment data.
        self.assertEqual(bridged["model_year"].value, "2026")

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


class TvCharacteristicNormalizationQualityDefectClosureTests(unittest.TestCase):
    """Production defect closure (live preview, LG 100MRGB96B6.ARUG --
    follow-up ticket after PR #85 was deployed): ``color`` must reject
    marketing/display-technology text, and ``hdmi_count``/``usb_count``
    must reject non-numeric text, rather than surviving normalization as
    garbage values. PR #85's own Bitrix mapping (article/gallery/
    refresh_rate_hz/vesa_mount property IDs) is untouched by this
    module -- see ``tests/test_bitrix_tv_product_write_contract_closure.py``
    for that regression coverage."""

    def test_color_rejects_the_exact_marketing_phrase_seen_in_production(self):
        value, unit = normalize_characteristic_value(
            "color", "Основные цвета RGB Ультра (Тройная 100% сертификация цвета)"
        )
        self.assertEqual(value, "")
        self.assertEqual(unit, "")

    def test_color_rejects_other_marketing_style_text_with_digits_or_percent_or_parens(self):
        for marketing_text in (
            "Технология Quantum Dot (Ultra 100%)",
            "Расширенная цветовая гамма 95% DCI-P3",
            "Тройная сертификация (2026)",
        ):
            with self.subTest(marketing_text=marketing_text):
                value, _unit = normalize_characteristic_value("color", marketing_text)
                self.assertEqual(value, "")

    def test_valid_physical_color_survives_normalization(self):
        for physical_color in ("черный", "белый", "серебристый", "Space Gray", "темно-серый металлик"):
            with self.subTest(physical_color=physical_color):
                value, _unit = normalize_characteristic_value("color", physical_color)
                self.assertEqual(value, physical_color)

    def test_hdmi_count_rejects_nonnumeric_production_value(self):
        value, unit = normalize_characteristic_value("hdmi_count", "вход")
        self.assertEqual(value, "")
        self.assertEqual(unit, "")

    def test_hdmi_count_rejects_other_nonnumeric_text(self):
        for garbage in ("HDMI", "есть", "да", "нет"):
            with self.subTest(garbage=garbage):
                value, _unit = normalize_characteristic_value("hdmi_count", garbage)
                self.assertEqual(value, "")

    def test_usb_count_rejects_nonnumeric_production_value(self):
        value, unit = normalize_characteristic_value("usb_count", "камеры")
        self.assertEqual(value, "")
        self.assertEqual(unit, "")

    def test_valid_numeric_port_counts_survive_normalization(self):
        self.assertEqual(normalize_characteristic_value("hdmi_count", "4")[0], "4")
        self.assertEqual(normalize_characteristic_value("hdmi_count", "3 x HDMI 2.1")[0], "3")
        self.assertEqual(normalize_characteristic_value("usb_count", "2")[0], "2")
        self.assertEqual(normalize_characteristic_value("usb_count", "2 (USB-A + USB-C)")[0], "2")

    def test_implausibly_large_count_is_rejected_not_guessed(self):
        # A mis-extracted 4-digit model-year-like number must never be
        # accepted as a port count.
        value, _unit = normalize_characteristic_value("hdmi_count", "2026")
        self.assertEqual(value, "")

    def test_valid_characteristics_unrelated_to_the_defects_remain_intact(self):
        # Production preview also had refresh_rate_hz/screen_diagonal_cm/
        # vesa_mount values that must NOT be affected by this closure.
        self.assertEqual(
            normalize_characteristic_value("refresh_rate_hz", "120Гц (VRR 165Гц)"),
            ("120Гц (VRR 165Гц)", "Hz"),
        )
        self.assertEqual(normalize_characteristic_value("screen_diagonal_cm", "254.0"), ("254.0", "cm"))
        self.assertEqual(normalize_characteristic_value("vesa_mount", "600 x 400"), ("600 x 400", "mm"))


if __name__ == "__main__":
    unittest.main()
