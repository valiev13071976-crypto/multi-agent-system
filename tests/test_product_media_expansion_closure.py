"""Image & Product Media Pipeline — applied expansion closure tests."""

from __future__ import annotations

import io
import tempfile
import unittest

from PIL import Image

from product_media.errors import (
    MEDIA_FACT_UNSUPPORTED,
    MEDIA_RECIPE_INVALID,
    MEDIA_RIGHTS_DENIED,
    MEDIA_RIGHTS_UNKNOWN,
    MediaError,
)
from product_media.platform_models import (
    MAX_GENERATION_ATTEMPTS,
    PLATFORM_SCHEMA_VERSION,
    RECIPE_PROFILE_VERSION,
    RIGHTS_OWNED,
    RIGHTS_THIRD_PARTY_RESTRICTED,
    RIGHTS_UNKNOWN,
    TARGET_PROFILE_VERSION,
    MediaRights,
)
from product_media.profiles import get_target_profile, list_marketplace_profiles
from product_media.recipes import OPERATION_REGISTRY, build_recipe, recipe_identity
from product_media.service import ProductMediaService
from product_media.sqlite_store import SqliteMediaStore
from product_media.video import FakeVideoRenderer, build_video_recipe


def _png(w: int = 400, h: int = 400, color=(80, 120, 40)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _svc() -> ProductMediaService:
    return ProductMediaService(store=SqliteMediaStore(":memory:"))


class ContractTests(unittest.TestCase):
    def test_schema_and_registry(self):
        self.assertEqual(PLATFORM_SCHEMA_VERSION, "1.0.0")
        self.assertTrue(RECIPE_PROFILE_VERSION)
        self.assertTrue(TARGET_PROFILE_VERSION)
        self.assertIn("background_remove", OPERATION_REGISTRY)
        self.assertIn("resize", OPERATION_REGISTRY)


class ImmutableSourceTests(unittest.TestCase):
    def test_ingest_preserves_source_hash_and_lineage(self):
        svc = _svc()
        data = _png(300, 200)
        v1 = svc.ingest(tenant_id="tenant-a", data=data, filename="p.png")
        self.assertTrue(v1.source_content_hash)
        self.assertEqual(len(v1.source_content_hash), 64)
        v2 = svc.transform_resize(tenant_id="tenant-a", version_id=v1.version_id, width=150, height=100)
        self.assertEqual(v2.parent_version_id, v1.version_id)
        self.assertEqual(v2.media_id, v1.media_id)
        # Original blob unchanged
        self.assertEqual(svc.get(tenant_id="tenant-a", version_id=v1.version_id).content_hash, v1.content_hash)


class RecipeTests(unittest.TestCase):
    def test_recipe_rejects_executable_params(self):
        with self.assertRaises(MediaError) as ctx:
            build_recipe(tenant_id="tenant-a", operations=[{"name": "resize", "parameters": {"eval": "1"}}])
        self.assertEqual(ctx.exception.code, MEDIA_RECIPE_INVALID)

    def test_idempotent_recipe_apply(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(500, 500), filename="a.png")
        recipe = build_recipe(
            tenant_id="tenant-a",
            operations=[{"name": "resize", "parameters": {"width": 256, "height": 256, "fit": "pad"}}],
            target_profile_id="website_thumbnail",
        )
        a = svc.apply_recipe(tenant_id="tenant-a", version_id=v.version_id, recipe=recipe)
        b = svc.apply_recipe(tenant_id="tenant-a", version_id=v.version_id, recipe=recipe)
        self.assertEqual(a.version_id, b.version_id)
        self.assertEqual(a.recipe_id, recipe.recipe_id)
        self.assertEqual(
            recipe_identity(source_hash=v.content_hash, recipe=recipe),
            recipe_identity(source_hash=v.content_hash, recipe=recipe),
        )


class BackgroundReplaceTests(unittest.TestCase):
    def test_background_remove_then_replace(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(), filename="p.png")
        cut = svc.remove_background(tenant_id="tenant-a", version_id=v.version_id)
        self.assertEqual(cut.provider_id, "fake-bg-remove")
        replaced = svc.replace_background(tenant_id="tenant-a", version_id=cut.version_id, mode="solid")
        self.assertEqual(replaced.parent_version_id, cut.version_id)
        self.assertEqual(replaced.operation, "background_replace")


class EnhancementFidelityTests(unittest.TestCase):
    def test_deterministic_enhance_and_generative_review(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(), filename="p.png")
        e1 = svc.enhance(tenant_id="tenant-a", version_id=v.version_id, generative=False)
        self.assertEqual(e1.metadata_safe.get("generative"), False)
        e2 = svc.enhance(tenant_id="tenant-a", version_id=v.version_id, generative=True)
        self.assertEqual(e2.status, "review_required")
        self.assertTrue(e2.metadata_safe.get("fidelity_review_required"))


class InfographicTests(unittest.TestCase):
    def test_fact_lock_and_invented_claim_rejected(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(800, 800), filename="p.png")
        out = svc.render_infographic(
            tenant_id="tenant-a",
            version_id=v.version_id,
            product_facts={"name": "Widget", "sku": "W-1", "price": "19.99"},
        )
        self.assertEqual(out.operation, "infographic")
        self.assertIn("price", out.metadata_safe.get("facts_used") or [])
        with self.assertRaises(MediaError) as ctx:
            svc.render_infographic(
                tenant_id="tenant-a",
                version_id=v.version_id,
                product_facts={"name": "X"},
                title="Certified ISO magic discount 90%",
            )
        self.assertEqual(ctx.exception.code, MEDIA_FACT_UNSUPPORTED)


class MarketplaceProfileTests(unittest.TestCase):
    def test_wb_ozon_yandex_same_core(self):
        profiles = list_marketplace_profiles()
        self.assertEqual(len(profiles), 3)
        channels = {p.channel for p in profiles}
        self.assertEqual(channels, {"wildberries", "ozon", "yandex_market"})
        for p in profiles:
            self.assertEqual(p.source_of_rules, "configurable")
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(1200, 1200), filename="p.png")
        out = svc.render_marketplace_set(tenant_id="tenant-a", version_id=v.version_id)
        self.assertIn("wb_main", out)
        self.assertIn("ozon_main", out)
        self.assertIn("yandex_market_main", out)
        wb = svc.get(tenant_id="tenant-a", version_id=out["wb_main"])
        self.assertEqual(wb.target_profile_id, "wb_main")
        self.assertTrue(wb.recipe_id)
        self.assertTrue(wb.parent_version_id or wb.recipe_id)


class WebsiteSocialBannerTests(unittest.TestCase):
    def test_website_social_banner_profiles(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(1200, 1200), filename="p.png")
        hero = svc.render_for_profile(tenant_id="tenant-a", version_id=v.version_id, profile_id="website_hero")
        square = svc.render_for_profile(tenant_id="tenant-a", version_id=v.version_id, profile_id="social_square")
        banner = svc.render_banner(tenant_id="tenant-a", version_id=v.version_id, cta="Shop now")
        self.assertEqual(hero.target_profile_id, "website_hero")
        self.assertEqual(square.width, get_target_profile("social_square").width)
        self.assertTrue(banner.version_id)


class ProductVideoTests(unittest.TestCase):
    def test_video_recipe_and_fake_renderer(self):
        svc = _svc()
        recipe = svc.create_video_recipe(
            tenant_id="tenant-a",
            scenes=[{"start_sec": 0, "end_sec": 3, "text_overlay": "Hook"}, {"start_sec": 3, "end_sec": 10}],
            duration_sec=10,
            rights_status=RIGHTS_OWNED,
        )
        asset = svc.render_video(tenant_id="tenant-a", recipe=recipe)
        self.assertEqual(asset.media_type, "video")
        self.assertTrue(asset.metadata_safe.get("fake"))
        self.assertEqual(asset.provider_id, "fake-video-render")
        self.assertEqual(asset.recipe_id, recipe.recipe_id)

    def test_restricted_rights_block_render(self):
        recipe = build_video_recipe(
            tenant_id="tenant-a",
            scenes=[{"start_sec": 0, "end_sec": 2}],
            rights_status=RIGHTS_THIRD_PARTY_RESTRICTED,
        )
        with self.assertRaises(MediaError) as ctx:
            FakeVideoRenderer().render(recipe=recipe)
        self.assertEqual(ctx.exception.code, MEDIA_RIGHTS_DENIED)


class RightsTests(unittest.TestCase):
    def test_unknown_rights_block_export(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(), filename="p.png")
        with self.assertRaises(MediaError) as ctx:
            svc.assert_export_rights(tenant_id="tenant-a", version_id=v.version_id)
        self.assertEqual(ctx.exception.code, MEDIA_RIGHTS_UNKNOWN)
        svc.set_rights(
            tenant_id="tenant-a",
            version_id=v.version_id,
            rights=MediaRights(rights_id="r1", tenant_id="tenant-a", status=RIGHTS_OWNED),
        )
        ok = svc.assert_export_rights(tenant_id="tenant-a", version_id=v.version_id)
        self.assertEqual(ok.status, RIGHTS_OWNED)


class ContentFactoryHandoffTests(unittest.TestCase):
    def test_media_brief_recipe_handoff(self):
        svc = _svc()
        recipe = svc.recipe_from_media_brief(tenant_id="tenant-a", aspect_ratio="1:1")
        self.assertTrue(recipe.operations)
        result = svc.generate_from_brief(
            tenant_id="tenant-a",
            scene_description="product hero",
            media_brief_id="brief-1",
            bulk=True,
        )
        self.assertTrue(result["version_ids"])


class BulkCancelNoLoopTests(unittest.TestCase):
    def test_cancel_preserves_checkpoint(self):
        svc = _svc()
        items = [_png(64, 64) for _ in range(5)]
        out = svc.bulk_ingest(tenant_id="tenant-a", items=items)
        cancelled = svc.cancel_job(tenant_id="tenant-a", job_id=out["job_id"])
        self.assertEqual(cancelled.status, "cancelled")
        self.assertGreaterEqual(cancelled.checkpoint, 0)

    def test_bounded_generate_terminates(self):
        svc = _svc()
        result = svc.bounded_generate(
            tenant_id="tenant-a",
            scene_description="x",
            max_attempts=MAX_GENERATION_ATTEMPTS,
            max_quality_retries=1,
            variant_count=1,
        )
        self.assertTrue(result["terminated"])
        self.assertLessEqual(result["attempts"], MAX_GENERATION_ATTEMPTS)


class TenantIsolationTests(unittest.TestCase):
    def test_cross_tenant_get_denied(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(), filename="p.png")
        self.assertIsNone(svc.get(tenant_id="tenant-b", version_id=v.version_id))


if __name__ == "__main__":
    unittest.main()
