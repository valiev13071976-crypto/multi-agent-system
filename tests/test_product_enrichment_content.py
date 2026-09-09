"""Product enrichment pipeline: fact-only, Russian-default content
generation contract (requirement 4) -- every factual claim in generated
content must be supported by a verified/probable enrichment fact, never
invented."""

from __future__ import annotations

import unittest

from product_enrichment.content import generate_content
from product_enrichment.identity import resolve_identity
from product_enrichment.models import (
    CONFIDENCE_CONFLICTING,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_UNVERIFIED,
    CONFIDENCE_VERIFIED,
    NormalizedCharacteristic,
    ProductIdentityQuery,
)


def _identity():
    return resolve_identity(
        ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG", ean="8806096824788", category="CE", subcategory="TV")
    )


class GenerateContentTests(unittest.TestCase):
    def test_no_characteristics_yields_minimal_but_valid_content(self):
        draft = generate_content(_identity(), {})
        self.assertEqual(draft.short_description, "LG 55MRGB86B6A.ARUG.")
        self.assertEqual(draft.detailed_description, "LG 55MRGB86B6A.ARUG.")
        self.assertEqual(draft.facts_used, ())
        self.assertIn("LG", draft.seo_keywords)
        self.assertIn("55MRGB86B6A.ARUG", draft.seo_keywords)

    def test_verified_characteristic_produces_phrase_and_is_tracked_in_facts_used(self):
        chars = {
            "screen_diagonal_cm": NormalizedCharacteristic(
                key="screen_diagonal_cm", value="139.7", unit="cm", confidence=CONFIDENCE_VERIFIED
            )
        }
        draft = generate_content(_identity(), chars)
        self.assertIn("139.7", draft.short_description)
        self.assertIn("screen_diagonal_cm", draft.facts_used)

    def test_probable_characteristic_is_usable_too(self):
        chars = {
            "color": NormalizedCharacteristic(key="color", value="черный", confidence=CONFIDENCE_PROBABLE)
        }
        draft = generate_content(_identity(), chars)
        self.assertIn("черный", draft.short_description)

    def test_unverified_characteristic_never_appears_in_generated_text(self):
        chars = {
            "color": NormalizedCharacteristic(key="color", value="INVENTED_COLOR", confidence=CONFIDENCE_UNVERIFIED)
        }
        draft = generate_content(_identity(), chars)
        self.assertNotIn("INVENTED_COLOR", draft.short_description)
        self.assertNotIn("INVENTED_COLOR", draft.detailed_description)
        self.assertNotIn("color", draft.facts_used)

    def test_conflicting_characteristic_never_appears_in_generated_text(self):
        chars = {
            "color": NormalizedCharacteristic(key="color", value="CONFLICT_VALUE", confidence=CONFIDENCE_CONFLICTING)
        }
        draft = generate_content(_identity(), chars)
        self.assertNotIn("CONFLICT_VALUE", draft.short_description)
        self.assertNotIn("CONFLICT_VALUE", draft.detailed_description)

    def test_characteristic_with_no_phrase_template_is_not_hallucinated_into_text(self):
        # "usb_count" has no _PHRASE_TEMPLATES entry -- must not silently
        # invent wording for it.
        chars = {
            "usb_count": NormalizedCharacteristic(key="usb_count", value="3", confidence=CONFIDENCE_VERIFIED)
        }
        draft = generate_content(_identity(), chars)
        self.assertNotIn("usb_count", draft.facts_used)

    def test_seo_description_bounded_to_160_chars(self):
        chars = {
            key: NormalizedCharacteristic(key=key, value="x" * 20, confidence=CONFIDENCE_VERIFIED)
            for key in (
                "screen_diagonal_cm",
                "screen_resolution",
                "panel_technology",
                "backlight_technology",
                "refresh_rate_hz",
                "hdr_formats",
            )
        }
        draft = generate_content(_identity(), chars)
        self.assertLessEqual(len(draft.seo_description), 160)

    def test_seo_keywords_include_category_and_subcategory_and_are_deduplicated(self):
        draft = generate_content(_identity(), {})
        self.assertIn("CE", draft.seo_keywords)
        self.assertIn("TV", draft.seo_keywords)
        self.assertEqual(len(draft.seo_keywords), len(set(draft.seo_keywords)))

    def test_image_alt_and_title_equal_product_name(self):
        draft = generate_content(_identity(), {})
        self.assertEqual(draft.image_alt, "LG 55MRGB86B6A.ARUG")
        self.assertEqual(draft.image_title, "LG 55MRGB86B6A.ARUG")


if __name__ == "__main__":
    unittest.main()
