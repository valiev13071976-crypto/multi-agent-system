"""Product enrichment pipeline: complete preview (requirement 11) --
identity/classification/commerce/content/physical/characteristics/media/
SEO plus READY TO WRITE / NOT WRITABLE / MISSING SOURCE DATA. Zero Bitrix
mutation anywhere in this module."""

from __future__ import annotations

import unittest

from product_enrichment.identity import resolve_identity
from product_enrichment.models import (
    CONFIDENCE_VERIFIED,
    ContentDraft,
    EnrichmentResult,
    IdentityConflict,
    MediaAsset,
    MediaResult,
    NormalizedCharacteristic,
    ProductIdentityQuery,
)
from product_enrichment.preview import enrichment_preview_dict, format_enrichment_preview_text


def _identity():
    return resolve_identity(
        ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG", ean="8806096824788", category="CE", subcategory="TV")
    )


class EnrichmentPreviewDictTests(unittest.TestCase):
    def test_minimal_result_reports_missing_characteristics_content_and_media(self):
        result = EnrichmentResult(identity=_identity())
        data = enrichment_preview_dict(result)
        self.assertIn("identity", data["ready_to_write"])
        self.assertIn("characteristics", data["missing_source_data"])
        self.assertIn("content", data["missing_source_data"])
        self.assertIn("media", data["missing_source_data"])

    def test_complete_result_reports_ready_to_write_for_every_section(self):
        characteristics = {
            "screen_diagonal_cm": NormalizedCharacteristic(
                key="screen_diagonal_cm", value="139", unit="cm", confidence=CONFIDENCE_VERIFIED
            )
        }
        content = ContentDraft(short_description="LG 55MRGB86B6A.ARUG — телевизор.", seo_title="LG TV — купить")
        media = MediaResult(
            assets=(
                MediaAsset(
                    role="preview",
                    content_hash="abc",
                    filename="x.jpg",
                    base64_content="AAAA",
                    width=300,
                    height=300,
                    mime_type="image/jpeg",
                    source_url="https://lg.com/x.jpg",
                    source_type="manufacturer",
                ),
            ),
            status=MediaResult.STATUS_READY,
        )
        result = EnrichmentResult(identity=_identity(), characteristics=characteristics, content=content, media=media)
        data = enrichment_preview_dict(result)
        self.assertEqual(
            set(data["ready_to_write"]), {"identity", "characteristics", "content", "media"}
        )
        self.assertEqual(data["missing_source_data"], [])
        self.assertTrue(data["media"]["main_image_prepared"])
        self.assertEqual(data["characteristics"]["count"], 1)
        self.assertEqual(data["characteristics"]["verified_count"], 1)

    def test_conflict_reported_under_not_writable(self):
        result = EnrichmentResult(
            identity=_identity(),
            conflicts=(IdentityConflict(code="characteristic_conflict_color", detail="x"),),
        )
        data = enrichment_preview_dict(result)
        self.assertIn("conflict:characteristic_conflict_color", data["not_writable_or_unresolved"])

    def test_research_unavailable_is_reported_under_missing_source_data(self):
        result = EnrichmentResult(identity=_identity(), research_available=False)
        data = enrichment_preview_dict(result)
        self.assertTrue(any("research" in item for item in data["missing_source_data"]))

    def test_cache_hit_flag_is_surfaced(self):
        result = EnrichmentResult(identity=_identity(), cache_hit=True)
        data = enrichment_preview_dict(result)
        self.assertTrue(data["cache_hit"])

    def test_important_characteristics_limited_to_five(self):
        characteristics = {
            f"key_{i}": NormalizedCharacteristic(key=f"key_{i}", value=str(i), confidence=CONFIDENCE_VERIFIED)
            for i in range(10)
        }
        result = EnrichmentResult(identity=_identity(), characteristics=characteristics)
        data = enrichment_preview_dict(result)
        self.assertEqual(len(data["characteristics"]["important"]), 5)
        self.assertEqual(data["characteristics"]["count"], 10)


class FormatEnrichmentPreviewTextTests(unittest.TestCase):
    def test_text_never_mentions_a_bare_write_and_states_no_mutation_gate_elsewhere(self):
        result = EnrichmentResult(identity=_identity())
        text = format_enrichment_preview_text(result)
        self.assertIn("READY TO WRITE", text)
        self.assertIn("NOT WRITABLE / UNRESOLVED", text)
        self.assertIn("MISSING SOURCE DATA", text)
        self.assertIn("LG 55MRGB86B6A.ARUG", text)

    def test_ean_and_article_shown_when_present(self):
        result = EnrichmentResult(identity=_identity())
        text = format_enrichment_preview_text(result)
        self.assertIn("8806096824788", text)

    def test_cache_hit_note_present_when_reused(self):
        result = EnrichmentResult(identity=_identity(), cache_hit=True)
        text = format_enrichment_preview_text(result)
        self.assertIn("повторный поиск не выполнялся", text)


if __name__ == "__main__":
    unittest.main()
