"""Product enrichment pipeline: fail-closed identity resolution
(requirement 1)."""

from __future__ import annotations

import unittest

from product_enrichment.identity import (
    detect_variant_conflict,
    evidence_matches_identity,
    extract_variant_tokens,
    resolve_identity,
)
from product_enrichment.models import IdentityConflictError, ProductIdentityQuery, compute_identity_key


class ResolveIdentityTests(unittest.TestCase):
    def test_brand_and_model_and_ean_resolve_with_strongest_strength(self):
        identity = resolve_identity(
            ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG", ean="8806096824788", category="CE", subcategory="TV")
        )
        self.assertEqual(identity.brand, "LG")
        self.assertEqual(identity.model, "55MRGB86B6A.ARUG")
        self.assertEqual(identity.strength, "ean_and_model")
        self.assertEqual(identity.category, "CE")
        self.assertEqual(identity.subcategory, "TV")

    def test_article_used_as_model_when_model_not_supplied(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", article="32LQ63006LA.ARUG"))
        self.assertEqual(identity.model, "32LQ63006LA.ARUG")
        self.assertEqual(identity.strength, "article_only")

    def test_missing_brand_fails_closed(self):
        with self.assertRaises(IdentityConflictError) as ctx:
            resolve_identity(ProductIdentityQuery(brand="", model="X"))
        self.assertEqual(ctx.exception.code, "missing_brand")

    def test_missing_model_and_article_fails_closed(self):
        with self.assertRaises(IdentityConflictError) as ctx:
            resolve_identity(ProductIdentityQuery(brand="LG"))
        self.assertEqual(ctx.exception.code, "missing_model_or_article")

    def test_invalid_ean_format_fails_closed(self):
        with self.assertRaises(IdentityConflictError) as ctx:
            resolve_identity(ProductIdentityQuery(brand="LG", model="X", ean="not-a-valid-ean"))
        self.assertEqual(ctx.exception.code, "invalid_ean_format")

    def test_identity_key_is_deterministic_and_case_insensitive(self):
        a = resolve_identity(ProductIdentityQuery(brand="LG", model="X", ean="8806096824788"))
        b = resolve_identity(ProductIdentityQuery(brand="lg", model="x", ean="8806096824788"))
        self.assertEqual(a.identity_key, b.identity_key)
        self.assertEqual(a.identity_key, compute_identity_key(brand="LG", model="X", ean="8806096824788"))

    def test_different_model_yields_different_identity_key(self):
        a = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        b = resolve_identity(ProductIdentityQuery(brand="LG", model="65MRGB86B6A.ARUG"))
        self.assertNotEqual(a.identity_key, b.identity_key)


class EvidenceAssociationTests(unittest.TestCase):
    def test_exact_model_in_text_matches(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        self.assertTrue(evidence_matches_identity(identity, text="Обзор LG 55MRGB86B6A.ARUG", url=""))

    def test_similar_model_family_does_not_match(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        self.assertFalse(evidence_matches_identity(identity, text="Обзор LG 55MRGB86B6A.ARUB (другая модель)", url=""))

    def test_unrelated_page_does_not_match(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        self.assertFalse(evidence_matches_identity(identity, text="Samsung QLED обзор", url="https://example.com/samsung"))


class VariantConflictTests(unittest.TestCase):
    def test_screen_size_variant_mismatch_detected(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="TV-55-INCH-X1"))
        tokens = extract_variant_tokens("55-inch")
        self.assertEqual(tokens.get("screen_size_class"), "55")
        conflict = detect_variant_conflict(identity, text="This is the 65-inch variant of the same series")
        self.assertEqual(conflict, "variant_mismatch_screen_size_class")

    def test_matching_screen_size_is_not_a_conflict(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="TV-55-INCH-X1"))
        self.assertIsNone(detect_variant_conflict(identity, text="the 55-inch model specs"))

    def test_model_year_mismatch_detected(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="TV-2025-X1"))
        conflict = detect_variant_conflict(identity, text="the 2024 model year version")
        self.assertEqual(conflict, "variant_mismatch_model_year")

    def test_no_variant_tokens_present_is_not_a_conflict(self):
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        self.assertIsNone(detect_variant_conflict(identity, text="general marketing text with no size or year"))


if __name__ == "__main__":
    unittest.main()
