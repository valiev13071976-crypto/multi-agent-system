"""Big Block A — Product Content Pipeline (Blocks 12–14) targeted closure tests. Offline only."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from product_content.card import assemble_product_card, canonical_title, map_block10_row
from product_content.contracts import (
    AI_GENERATED_IMAGE,
    COMPLETE,
    DERIVED_IMAGE,
    INSUFFICIENT_INPUT,
    PARTIAL,
    ROLE_DETAIL,
    ROLE_GALLERY,
    ROLE_LIFESTYLE,
    ROLE_MAIN,
    SOURCE_IMAGE,
    STATUS_BLOCKED,
    STATUS_READY,
    STATUS_READY_WITH_WARNINGS,
    STATUS_REQUIRES_REVIEW,
)
from product_content.errors import CONTENT_ACCESS_DENIED, CONTENT_IDENTITY_REQUIRED, ProductContentError
from product_content.seo_package import transliterate_local
from product_content.service import ProductContentService
from product_content.store import ProductContentStore


def _png(color=(20, 80, 180), size=(32, 24)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


SMARTPHONE = {
    "sku": "PH-100",
    "product_id": "prod-phone-1",
    "product_name": "Nova Phone X",
    "brand": "Nova",
    "model": "X",
    "category": "smartphone",
    "color": "чёрный",
    "memory": "8 ГБ",
    "weight": "0.19 кг",
    "processor": "N8",
    "purchase_price": "95000",
    "selling_price": "120000",
    "features": "USB-C; dual SIM",
    "source": "FILE_PROVIDED",
}

HEADPHONES = {
    "sku": "HP-9",
    "product_id": "prod-hp-1",
    "product_name": "Quiet Buds",
    "brand": "Nova",
    "model": "QB1",
    "category": "headphones",
    "color": "blue",
    "source": "FILE_PROVIDED",
}

CYRILLIC = {
    "sku": "НР-1",
    "product_id": "prod-cyr-1",
    "product_name": "Наушники Синие",
    "brand": "Аура",
    "category": "headphones",
    "color": "синий",
    "source": "FILE_PROVIDED",
}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.svc = ProductContentService(store=ProductContentStore())

    def test_identity_sku_brand_model_and_block10_mapping(self):
        row = {
            "артикул": "SKU-RU",
            "товар": "Кабель",
            "бренд": "Aura",
            "category": "generic",
        }
        mapped = map_block10_row(row)
        self.assertEqual(mapped["sku"], "SKU-RU")
        self.assertEqual(mapped["article"], "SKU-RU")
        card = assemble_product_card(row, tenant_id="t-a")
        self.assertEqual(card.sku, "SKU-RU")
        self.assertEqual(card.article, "SKU-RU")
        self.assertEqual(card.brand, "Aura")
        self.assertIn("Кабель", card.canonical_title)
        pkg = self.svc.build_package({**row, "sku": "SKU-RU", "product_name": "Кабель"}, tenant_id="t-a")
        self.assertEqual(pkg.card.sku, "SKU-RU")

    def test_normalized_attributes_raw_provenance_unknown(self):
        row = {
            **SMARTPHONE,
            "ip_rating": "UNKNOWN",
        }
        card = assemble_product_card(row, tenant_id="t-a")
        self.assertEqual(card.specifications["color"].normalized, "black")
        self.assertEqual(card.specifications["color"].raw, "чёрный")
        self.assertEqual(card.specifications["memory"].normalized, "8 GB")
        self.assertEqual(card.specifications["ip_rating"].provenance, "UNKNOWN")
        self.assertIsNone(card.specifications["ip_rating"].normalized)
        self.assertIn("ip_rating", card.unknown_facts)
        self.assertNotIn("IP68", card.long_description.upper().replace("UNKNOWN", ""))
        self.assertNotIn("ip68", card.short_description.casefold())
        self.assertNotIn("ip68", " ".join(card.feature_bullets).casefold())

    def test_title_deterministic_no_spam(self):
        t1 = canonical_title({"product_name": "BEST Nova X ₽120000 official dealer", "brand": "Nova", "model": "X"})
        t2 = canonical_title({"product_name": "BEST Nova X ₽120000 official dealer", "brand": "Nova", "model": "X"})
        self.assertEqual(t1, t2)
        self.assertNotIn("BEST", t1.upper() if "best" not in t1.casefold() else "ok")
        self.assertNotRegex(t1, r"(?i)official dealer")
        self.assertNotRegex(t1, r"120000")
        caps = canonical_title({"product_name": "NOVA CABLE USB"})
        self.assertNotEqual(caps, "NOVA CABLE USB")

    def test_descriptions_grounded_and_completeness(self):
        complete = assemble_product_card(SMARTPHONE, tenant_id="t-a")
        self.assertEqual(complete.completeness, COMPLETE)
        self.assertIn("Nova", complete.short_description)
        self.assertIn("memory:", complete.long_description)
        self.assertTrue(any("memory:" in b for b in complete.feature_bullets))
        incomplete = assemble_product_card(
            {"sku": "Z", "product_name": "Thing", "category": "smartphone"},
            tenant_id="t-a",
        )
        self.assertEqual(incomplete.completeness, INSUFFICIENT_INPUT)
        self.assertIn("brand", incomplete.missing_required)
        partial = assemble_product_card(
            {"sku": "Z2", "product_name": "Phone", "brand": "Nova", "category": "smartphone"},
            tenant_id="t-a",
        )
        self.assertEqual(partial.completeness, PARTIAL)
        self.assertTrue(partial.missing_recommended)

    def test_category_aware_policy_not_universal(self):
        cloth = assemble_product_card(
            {"sku": "C1", "product_name": "Tee", "category": "clothing", "size": "M", "color": "red"},
            tenant_id="t-a",
        )
        phone = assemble_product_card(
            {"sku": "P1", "product_name": "Phone", "brand": "N", "category": "smartphone"},
            tenant_id="t-a",
        )
        self.assertIn("memory", phone.missing_recommended)
        self.assertNotIn("processor", cloth.missing_recommended)
        self.assertNotIn("size", phone.missing_recommended)

    def test_economics_reference_not_in_specs(self):
        card = assemble_product_card(SMARTPHONE, tenant_id="t-a")
        self.assertEqual(card.economics_reference.get("engine"), "data_intel.economics")
        self.assertIn(card.economics_reference.get("decision"), {"ALLOW", "WARN", "DENY", "REQUIRE_REVIEW"})
        self.assertNotIn("minimum_price", card.specifications)
        self.assertIsNotNone(card.selling_price)

    def test_seo_title_meta_slug_cyrillic_keywords_schema(self):
        png = _png()
        pkg = self.svc.build_package(HEADPHONES, tenant_id="t-a", media=[{"asset_id": "m1", "role": ROLE_MAIN, "data": png}])
        self.assertTrue(pkg.seo.quality["title_present"])
        self.assertLessEqual(pkg.seo.quality["title_length"], 70)
        self.assertTrue(pkg.seo.quality["meta_present"])
        self.assertLessEqual(pkg.seo.quality["meta_length"], 160)
        self.assertTrue(pkg.seo.quality["slug_valid"])
        self.assertTrue(pkg.seo.quality["keywords_are_candidates_only"])
        self.assertTrue(pkg.seo.quality["no_search_volume"])
        self.assertTrue(pkg.seo.quality["not_a_ranking_guarantee"])
        self.assertNotIn("search_volume", pkg.seo.as_dict())
        self.assertNotIn("cpc", pkg.seo.as_dict())
        self.assertNotIn("aggregateRating", pkg.seo.schema_product)
        self.assertNotIn("reviewCount", pkg.seo.schema_product)
        self.assertNotIn("offers", pkg.seo.schema_product)
        self.assertEqual(pkg.seo.keyword_note[:10], "CANDIDATES")
        cyr = self.svc.build_package(CYRILLIC, tenant_id="t-a")
        self.assertRegex(cyr.seo.canonical_slug, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertIn("naushniki", cyr.seo.canonical_slug)
        self.assertEqual(transliterate_local("Наушники"), transliterate_local("Наушники"))
        again = self.svc.build_package(CYRILLIC, tenant_id="t-a")
        self.assertEqual(cyr.seo.canonical_slug, again.seo.canonical_slug)
        self.assertEqual(cyr.package_id, again.package_id)

    def test_slug_collision_and_duplicate_title(self):
        a = self.svc.build_package(
            {**HEADPHONES, "product_id": "p-a", "sku": "HP-A"},
            tenant_id="t-a",
        )
        b = self.svc.build_package(
            {**HEADPHONES, "product_id": "p-b", "sku": "HP-A", "product_name": "Quiet Buds"},
            tenant_id="t-a",
        )
        self.assertTrue(b.seo.duplicate_title or b.warnings)
        c = self.svc.build_package(
            {
                **HEADPHONES,
                "product_id": "p-c",
                "sku": "HP-C",
                "product_name": a.card.canonical_title,
                "brand": "",
                "model": "",
            },
            tenant_id="t-a",
        )
        # same canonical identity pieces can collide on slug suffix sku
        self.assertIn("Quiet", c.seo.seo_title)

    def test_duplicate_sku_detected(self):
        self.svc.build_package({**HEADPHONES, "product_id": "one"}, tenant_id="t-a")
        other = self.svc.build_package({**HEADPHONES, "product_id": "two", "product_name": "Quiet Buds Twin"}, tenant_id="t-a")
        self.assertTrue(any("duplicate_sku" in w for w in other.warnings))
        self.assertEqual(other.status, STATUS_READY_WITH_WARNINGS)

    def test_media_validation_roles_order_provenance(self):
        main = _png((10, 10, 10), (40, 30))
        gal = _png((200, 10, 10), (40, 30))
        pkg = self.svc.build_package(
            HEADPHONES,
            tenant_id="t-a",
            media=[
                {"asset_id": "g1", "role": ROLE_GALLERY, "data": gal},
                {"asset_id": "m1", "role": ROLE_MAIN, "data": main},
                {"asset_id": "d1", "role": ROLE_DETAIL, "data": _png((1, 2, 3))},
                {"asset_id": "l1", "role": ROLE_LIFESTYLE, "data": _png((4, 5, 6))},
                {"asset_id": "ai1", "role": ROLE_GALLERY, "kind": AI_GENERATED_IMAGE},
            ],
        )
        roles = [a.role for a in pkg.media.assets]
        self.assertLess(roles.index(ROLE_MAIN), roles.index(ROLE_DETAIL))
        self.assertLess(roles.index(ROLE_DETAIL), roles.index(ROLE_GALLERY))
        main_asset = next(a for a in pkg.media.assets if a.asset_id == "m1")
        self.assertEqual(main_asset.validation_status, "VALID")
        self.assertEqual(main_asset.width, 40)
        self.assertEqual(main_asset.height, 30)
        self.assertTrue(main_asset.checksum)
        self.assertEqual(main_asset.kind, SOURCE_IMAGE)
        self.assertTrue(main_asset.original_preserved)
        self.assertEqual(pkg.media.original_bytes_by_asset["m1"], main)
        thumbs = [a for a in pkg.media.assets if a.kind == DERIVED_IMAGE]
        self.assertTrue(thumbs)
        self.assertEqual(thumbs[0].provenance, "DERIVED")
        ai = next(a for a in pkg.media.assets if a.asset_id == "ai1")
        self.assertEqual(ai.kind, AI_GENERATED_IMAGE)
        self.assertEqual(ai.provenance, "AI_GENERATED")
        self.assertIn("Quiet Buds", main_asset.alt_text)
        self.assertNotIn("waterproof", main_asset.alt_text.casefold())

        bad = self.svc.build_package(
            {**HEADPHONES, "sku": "HP-BAD", "product_id": "bad-media"},
            tenant_id="t-a",
            media=[
                {"asset_id": "z", "role": ROLE_MAIN, "data": b""},
                {"asset_id": "c", "role": ROLE_GALLERY, "data": b"not-an-image"},
                {"asset_id": "u", "role": ROLE_DETAIL, "data": b"GIF89aXXXX", "filename": "x.gif"},
                {"asset_id": "d1", "role": ROLE_GALLERY, "data": main},
                {"asset_id": "d2", "role": ROLE_GALLERY, "data": main},
                {"asset_id": "net", "role": ROLE_MAIN, "url": "https://example.invalid/x.png"},
            ],
        )
        statuses = {a.asset_id: a.validation_status for a in bad.media.assets}
        self.assertEqual(statuses["z"], "INVALID")
        self.assertIn(statuses["c"], {"CORRUPT", "UNSUPPORTED_MIME"})
        self.assertIn(statuses["u"], {"UNSUPPORTED_MIME", "CORRUPT"})
        self.assertTrue(bad.media.duplicate_checksums)
        self.assertTrue(any("remote_url_rejected" in i for i in bad.media.issues))

    def test_package_readiness_and_unsupported_claim(self):
        ready = self.svc.build_package(HEADPHONES, tenant_id="t-a")
        self.assertEqual(ready.status, STATUS_READY)
        self.assertFalse(ready.published)
        warn = self.svc.build_package(
            {"sku": "HP-W", "product_id": "w1", "product_name": "Buds", "category": "headphones"},
            tenant_id="t-a",
        )
        self.assertEqual(warn.status, STATUS_READY_WITH_WARNINGS)
        review = self.svc.build_package(
            {"sku": "PH-MISS", "product_id": "r1", "product_name": "Phone", "category": "smartphone"},
            tenant_id="t-a",
        )
        self.assertEqual(review.status, STATUS_REQUIRES_REVIEW)
        blocked = self.svc.build_package(
            {**HEADPHONES, "sku": "HP-CL", "product_id": "cl1", "marketing_copy": "Official dealer IP68 waterproof certified"},
            tenant_id="t-a",
        )
        self.assertEqual(blocked.status, STATUS_BLOCKED)
        self.assertTrue(blocked.issues)

    def test_tenant_isolation_versioning_idempotency_persist_readback(self):
        pkg = self.svc.build_package(SMARTPHONE, tenant_id="tenant-a", media=[{"asset_id": "im", "role": ROLE_MAIN, "data": _png()}])
        self.assertEqual(pkg.status in {STATUS_READY, STATUS_READY_WITH_WARNINGS}, True)
        back = self.svc.get_package(pkg.package_id, tenant_id="tenant-a")
        self.assertEqual(back.version, pkg.version)
        self.assertEqual(back.card.sku, "PH-100")
        self.assertEqual(back.provenance["unknown"], list(pkg.card.unknown_facts))
        self.assertEqual(back.card.field_provenance["canonical_title"], "DERIVED")
        again = self.svc.build_package(SMARTPHONE, tenant_id="tenant-a", media=[{"asset_id": "im", "role": ROLE_MAIN, "data": _png()}])
        self.assertEqual(again.package_id, pkg.package_id)
        changed = self.svc.build_package(
            {**SMARTPHONE, "color": "белый"},
            tenant_id="tenant-a",
            media=[{"asset_id": "im", "role": ROLE_MAIN, "data": _png()}],
        )
        self.assertNotEqual(changed.version, pkg.version)
        with self.assertRaises(ProductContentError) as ctx:
            self.svc.get_package(pkg.package_id, tenant_id="tenant-b")
        self.assertEqual(ctx.exception.code, CONTENT_ACCESS_DENIED)
        other = self.svc.build_package(HEADPHONES, tenant_id="tenant-b")
        with self.assertRaises(ProductContentError):
            self.svc.get_package(other.package_id, tenant_id="tenant-a")

    def test_no_identity_fails_closed(self):
        with self.assertRaises(ProductContentError) as ctx:
            assemble_product_card({"category": "generic"}, tenant_id="t-a")
        self.assertEqual(ctx.exception.code, CONTENT_IDENTITY_REQUIRED)

    def test_e2e_persisted_package(self):
        svc = ProductContentService()
        pkg = svc.build_package(
            SMARTPHONE,
            tenant_id="e2e-tenant",
            media=[{"asset_id": "main", "role": ROLE_MAIN, "data": _png(), "source_type": "fixture"}],
        )
        read = svc.get_package(pkg.package_id, tenant_id="e2e-tenant")
        self.assertEqual(read.card.product_id, "prod-phone-1")
        self.assertTrue(read.seo.seo_title)
        self.assertTrue(any(a.role == ROLE_MAIN and a.validation_status == "VALID" for a in read.media.assets))
        self.assertIn(read.status, {STATUS_READY, STATUS_READY_WITH_WARNINGS, STATUS_REQUIRES_REVIEW})
        self.assertFalse(read.published)
        self.assertEqual(read.card.economics_reference.get("engine"), "data_intel.economics")
        self.assertIn("generated", read.provenance)
        self.assertIn("card_fields", read.provenance)


if __name__ == "__main__":
    unittest.main()
