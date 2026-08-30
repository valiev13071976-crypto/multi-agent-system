"""Block 10 Product Media Intelligence — closure tests."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from PIL import Image, ImageDraw

from content_intel.platform_models import STATUS_VALIDATED, ContentAssetVersion
from content_intel.service import ContentIntelligenceService
from content_intel.sqlite_store import SqliteContentStore
from product_media.access import sanitize_filename
from product_media.errors import (
    MEDIA_CROSS_TENANT,
    MEDIA_DELETED,
    MEDIA_PIXEL_LIMIT_EXCEEDED,
    MEDIA_PRODUCT_LINK_AMBIGUOUS,
    MEDIA_TYPE_MISMATCH,
    MediaBatchRequired,
    MediaError,
)
from product_media.platform_models import LINK_CANDIDATE, LINK_CONFIRMED, ROLE_HERO
from product_media.planner import assert_sync_media_allowed, classify_media_workload
from product_media.policy import MAX_SYNC_IMAGE_COUNT, MAX_SYNC_VARIANTS
from product_media.providers.fake import FailingVariantProvider
from product_media.service import ProductMediaService
from product_media.similarity import TenantSimilarityIndex
from product_media.sqlite_store import SqliteMediaStore
from product_media.validation import synthetic_decompression_bomb_header, validate_and_extract_image
from task_queue.lanes import LANE_BULK, classify_workload


def _png(w: int = 200, h: int = 200, color=(120, 80, 40)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(w: int = 200, h: int = 200) -> bytes:
    img = Image.new("RGB", (w, h), (10, 150, 10))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _pattern_png(seed: int, w: int = 400, h: int = 400) -> bytes:
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(20):
        draw.rectangle((i * 10, i * 5, i * 10 + 30, i * 5 + 30), fill=((seed + i * 17) % 255, (seed * 3 + i) % 255, (seed * 7 + i) % 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _webp(w: int = 200, h: int = 200) -> bytes:
    img = Image.new("RGB", (w, h), (10, 10, 150))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _svc(path: str | None = None) -> ProductMediaService:
    store = SqliteMediaStore(path or ":memory:")
    return ProductMediaService(store=store, similarity_index=TenantSimilarityIndex())


class IngestionTests(unittest.TestCase):
    def test_jpeg_png_webp_validation(self):
        svc = _svc()
        for data, ext in [(_png(), ".png"), (_jpeg(), ".jpg"), (_webp(), ".webp")]:
            version = svc.ingest(tenant_id="tenant-a", data=data, filename=f"x{ext}")
            self.assertEqual(version.media_type, "image")
            self.assertGreater(version.width, 0)

    def test_type_mismatch_rejected(self):
        svc = _svc()
        data = _png()
        with self.assertRaises(MediaError) as ctx:
            svc.ingest(tenant_id="tenant-a", data=data, filename="fake.jpg")
        self.assertEqual(ctx.exception.code, MEDIA_TYPE_MISMATCH)

    def test_corrupt_bytes_rejected(self):
        svc = _svc()
        with self.assertRaises(MediaError):
            svc.ingest(tenant_id="tenant-a", data=b"not-an-image", filename="x.png")

    def test_content_hash_and_metadata(self):
        svc = _svc()
        data = _png(300, 200)
        v1 = svc.ingest(tenant_id="tenant-a", data=data, filename="product.png")
        self.assertEqual(len(v1.content_hash), 64)
        self.assertEqual(v1.width, 300)
        self.assertAlmostEqual(v1.width / v1.height, 1.5, places=1)

    def test_decompression_bomb_rejected(self):
        svc = _svc()
        bomb = synthetic_decompression_bomb_header()
        with self.assertRaises(MediaError) as ctx:
            svc.ingest(tenant_id="tenant-a", data=bomb, filename="bomb.png")
        self.assertIn(ctx.exception.code, {MEDIA_PIXEL_LIMIT_EXCEEDED, "MEDIA_CORRUPT"})


class SecurityTests(unittest.TestCase):
    def test_path_traversal_filename_safe(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "media.bin")
        self.assertNotIn("..", sanitize_filename("../evil.png"))

    def test_payload_tenant_override_denied(self):
        svc = _svc()
        with self.assertRaises(MediaError) as ctx:
            svc.ingest(tenant_id="tenant-a", data=_png(), payload_tenant="tenant-b")
        self.assertEqual(ctx.exception.code, MEDIA_CROSS_TENANT)

    def test_malicious_metadata_remains_data(self):
        svc = _svc()
        data = _png()
        version = svc.ingest(
            tenant_id="tenant-a",
            data=data,
            filename="SYSTEM_ignore_rules.png",
        )
        analysis = svc.analyze(tenant_id="tenant-a", version_id=version.version_id)
        self.assertIn("report", analysis)
        self.assertEqual(version.tenant_id, "tenant-a")

    def test_no_raw_bytes_in_observability(self):
        svc = _svc()
        svc.ingest(tenant_id="tenant-a", data=_png())
        for name, meta in svc.obs.events:
            for v in meta.values():
                self.assertNotIsInstance(v, (bytes, bytearray))


class TenantIsolationTests(unittest.TestCase):
    def test_identical_bytes_separate_ownership(self):
        svc = _svc()
        data = _png(color=(1, 2, 3))
        va = svc.ingest(tenant_id="tenant-a", data=data)
        vb = svc.ingest(tenant_id="tenant-b", data=data)
        self.assertNotEqual(va.version_id, vb.version_id)
        self.assertIsNone(svc.get(tenant_id="tenant-a", version_id=vb.version_id))
        self.assertIsNone(svc.get(tenant_id="tenant-b", version_id=va.version_id))

    def test_similarity_never_cross_tenant(self):
        svc = _svc()
        data = _png(color=(5, 6, 7))
        va = svc.ingest(tenant_id="tenant-a", data=data)
        svc.ingest(tenant_id="tenant-b", data=data)
        results = svc.find_similar(tenant_id="tenant-a", version_id=va.version_id)
        for row in results:
            self.assertEqual(row["tenant_id"], "tenant-a")

    def test_cross_tenant_transform_denied(self):
        svc = _svc()
        vb = svc.ingest(tenant_id="tenant-b", data=_png())
        with self.assertRaises(MediaError):
            svc.thumbnail(tenant_id="tenant-a", version_id=vb.version_id)

    def test_cross_tenant_mask_denied(self):
        svc = _svc()
        src = svc.ingest(tenant_id="tenant-a", data=_png(100, 100))
        mask = svc.ingest(tenant_id="tenant-b", data=_png(100, 100, color=(0, 0, 0)))
        with self.assertRaises(MediaError):
            svc.edit(
                tenant_id="tenant-a",
                source_version_id=src.version_id,
                instruction="paint",
                mask_version_id=mask.version_id,
            )


class ProductLinkTests(unittest.TestCase):
    def test_confirmed_explicit_link(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png())
        link = svc.link_product(
            tenant_id="tenant-a",
            version_id=v.version_id,
            product_id="prod-1",
            sku="SKU-1",
            link_state=LINK_CONFIRMED,
            source="explicit",
        )
        self.assertEqual(link.link_state, LINK_CONFIRMED)

    def test_visual_only_cannot_confirm(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png())
        with self.assertRaises(MediaError) as ctx:
            svc.link_product(
                tenant_id="tenant-a",
                version_id=v.version_id,
                product_id="prod-x",
                link_state=LINK_CONFIRMED,
                source="visual_only",
            )
        self.assertEqual(ctx.exception.code, MEDIA_PRODUCT_LINK_AMBIGUOUS)


class QualityTests(unittest.TestCase):
    def test_marketplace_too_small(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(50, 50))
        analysis = svc.analyze(tenant_id="tenant-a", version_id=v.version_id, profile="marketplace")
        self.assertFalse(analysis["report"]["passed"])

    def test_website_profile_passes_medium_image(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(600, 600))
        analysis = svc.analyze(tenant_id="tenant-a", version_id=v.version_id, profile="website")
        self.assertTrue(analysis["report"]["passed"])


class SimilarityTests(unittest.TestCase):
    def test_exact_duplicate(self):
        svc = _svc()
        data = _png(color=(9, 9, 9))
        v1 = svc.ingest(tenant_id="tenant-a", data=data)
        v2 = svc.ingest(tenant_id="tenant-a", data=data)
        dups = svc.find_duplicates(tenant_id="tenant-a", version_id=v1.version_id)
        self.assertIn(v2.version_id, dups)

    def test_unrelated_images_not_exact_duplicate(self):
        svc = _svc()
        v1 = svc.ingest(tenant_id="tenant-a", data=_pattern_png(1))
        v2 = svc.ingest(tenant_id="tenant-a", data=_pattern_png(999))
        self.assertNotEqual(v1.content_hash, v2.content_hash)
        dups = svc.find_duplicates(tenant_id="tenant-a", version_id=v1.version_id)
        self.assertNotIn(v2.version_id, dups)

    def test_deleted_excluded_from_similarity(self):
        svc = _svc()
        data = _png(color=(3, 3, 3))
        v1 = svc.ingest(tenant_id="tenant-a", data=data)
        v2 = svc.ingest(tenant_id="tenant-a", data=data)
        svc.delete(tenant_id="tenant-a", version_id=v2.version_id)
        dups = svc.find_duplicates(tenant_id="tenant-a", version_id=v1.version_id)
        self.assertNotIn(v2.version_id, dups)


class TransformTests(unittest.TestCase):
    def test_resize_and_lineage(self):
        svc = _svc()
        v1 = svc.ingest(tenant_id="tenant-a", data=_png(400, 200))
        v2 = svc.transform_resize(tenant_id="tenant-a", version_id=v1.version_id, width=200, height=200)
        self.assertEqual(v2.parent_version_id, v1.version_id)
        self.assertNotEqual(v1.content_hash, v2.content_hash)
        self.assertEqual(svc.get(tenant_id="tenant-a", version_id=v1.version_id).content_hash, v1.content_hash)

    def test_invalid_crop_rejected(self):
        svc = _svc()
        v1 = svc.ingest(tenant_id="tenant-a", data=_png(100, 100))
        with self.assertRaises(MediaError):
            svc.transform_crop(
                tenant_id="tenant-a", version_id=v1.version_id, left=90, top=90, width=50, height=50
            )

    def test_thumbnail_and_strip_metadata(self):
        svc = _svc()
        v1 = svc.ingest(tenant_id="tenant-a", data=_png())
        thumb = svc.thumbnail(tenant_id="tenant-a", version_id=v1.version_id)
        stripped = svc.strip_metadata(tenant_id="tenant-a", version_id=v1.version_id)
        self.assertLessEqual(max(thumb.width, thumb.height), 256)
        self.assertNotEqual(stripped.version_id, v1.version_id)


class BackgroundTests(unittest.TestCase):
    def test_background_removal_creates_version(self):
        svc = _svc()
        v1 = svc.ingest(tenant_id="tenant-a", data=_png())
        v2 = svc.remove_background(tenant_id="tenant-a", version_id=v1.version_id)
        self.assertEqual(v2.operation, "remove_background")
        self.assertEqual(v2.parent_version_id, v1.version_id)


class GenerationTests(unittest.TestCase):
    def test_generate_bounded_variants(self):
        svc = _svc()
        result = svc.generate_from_brief(
            tenant_id="tenant-a",
            scene_description="product hero shot",
            variant_count=2,
        )
        self.assertEqual(len(result["version_ids"]), 2)

    def test_variant_limit_enforced(self):
        svc = _svc()
        with self.assertRaises(MediaBatchRequired):
            svc.generate_from_brief(
                tenant_id="tenant-a",
                scene_description="many",
                variant_count=MAX_SYNC_VARIANTS + 1,
            )

    def test_partial_variant_failure(self):
        svc = ProductMediaService(
            store=SqliteMediaStore(":memory:"),
            generator=FailingVariantProvider(fail_on={2}),
        )
        result = svc.generate_from_brief(
            tenant_id="tenant-a",
            scene_description="variants",
            variant_count=3,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(result["version_ids"]), 2)


class EditTests(unittest.TestCase):
    def test_edit_creates_new_version(self):
        svc = _svc()
        src = svc.ingest(tenant_id="tenant-a", data=_png())
        edited = svc.edit(tenant_id="tenant-a", source_version_id=src.version_id, instruction="add label")
        self.assertEqual(edited.parent_version_id, src.version_id)

    def test_mask_dimension_mismatch(self):
        svc = _svc()
        src = svc.ingest(tenant_id="tenant-a", data=_png(100, 100))
        mask = svc.ingest(tenant_id="tenant-a", data=_png(50, 50))
        with self.assertRaises(MediaError):
            svc.edit(
                tenant_id="tenant-a",
                source_version_id=src.version_id,
                instruction="x",
                mask_version_id=mask.version_id,
            )


class MediaSetTests(unittest.TestCase):
    def test_missing_primary_role(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(1200, 1200))
        media_set = svc.validate_set(
            tenant_id="tenant-a",
            product_id="prod-1",
            items=[{"role": "detail", "version_id": v.version_id}],
            profile="marketplace",
        )
        self.assertIn("missing_primary", media_set.validation_errors)

    def test_valid_set_with_hero(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(1200, 1200))
        media_set = svc.validate_set(
            tenant_id="tenant-a",
            product_id="prod-1",
            items=[{"role": ROLE_HERO, "version_id": v.version_id}],
            profile="website",
        )
        self.assertEqual(len(media_set.validation_errors), 0)


class VideoTests(unittest.TestCase):
    def test_bounded_frame_sampling(self):
        svc = _svc()
        result = svc.analyze_video(
            tenant_id="tenant-a",
            data=b"\x00" * 100,
            duration_sec=30,
            width=640,
            height=360,
        )
        self.assertLessEqual(result["sampled_frames"], 30)
        self.assertIn("poster_version_id", result)

    def test_heavy_video_requires_batch(self):
        svc = _svc()
        with self.assertRaises(MediaBatchRequired):
            svc.analyze_video(
                tenant_id="tenant-a",
                data=b"\x00" * 100,
                duration_sec=300,
                bulk=False,
            )


class DeletionTests(unittest.TestCase):
    def test_deleted_unavailable(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png())
        svc.delete(tenant_id="tenant-a", version_id=v.version_id)
        self.assertIsNone(svc.get(tenant_id="tenant-a", version_id=v.version_id))

    def test_stale_transform_cannot_resurrect(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png())
        job_id = svc.queue_transform(tenant_id="tenant-a", version_id=v.version_id, operation="thumbnail")
        svc.delete(tenant_id="tenant-a", version_id=v.version_id)
        with self.assertRaises(MediaError) as ctx:
            svc.execute_pending_transform(tenant_id="tenant-a", job_id=job_id)
        self.assertEqual(ctx.exception.code, MEDIA_DELETED)


class WorkloadTests(unittest.TestCase):
    def test_bulk_classification(self):
        self.assertEqual(classify_media_workload(image_count=MAX_SYNC_IMAGE_COUNT + 1), "bulk")

    def test_interactive_cannot_downgrade_bulk(self):
        with self.assertRaises(MediaBatchRequired):
            assert_sync_media_allowed(image_count=MAX_SYNC_IMAGE_COUNT + 1, interactive=True)

    def test_media_large_lane(self):
        wl = classify_workload(metadata={"trusted_job_type": "media_large"})
        self.assertEqual(wl.lane, LANE_BULK)

    def test_bulk_ingest_checkpoint_resume(self):
        svc = _svc()
        items = [_png(color=(i, i, i)) for i in range(15)]
        first = svc.bulk_ingest(tenant_id="tenant-a", items=items, resume_from=0)
        second = svc.bulk_ingest(
            tenant_id="tenant-a",
            items=items,
            resume_from=first["checkpoint"],
            job_id=first["job_id"],
        )
        self.assertGreater(first["checkpoint"], 0)
        self.assertGreaterEqual(second["checkpoint"], first["checkpoint"])


class ContentFactoryIntegrationTests(unittest.TestCase):
    def test_media_brief_to_product_media(self):
        content_store = SqliteContentStore(":memory:")
        media_svc = _svc(":memory:")
        content_svc = ContentIntelligenceService(content_store, product_media_service=media_svc)
        asset = ContentAssetVersion(
            asset_id="asset-1",
            version_id="asset-v1",
            tenant_id="tenant-a",
            project_id="proj-1",
            content_type="social_post",
            channel="social",
            body="Buy our product",
            status=STATUS_VALIDATED,
            version_num=1,
        )
        content_store.save_asset(asset)
        brief = content_svc.create_media_brief(
            tenant_id="tenant-a",
            asset_version_id=asset.version_id,
            media_type="image",
            aspect_ratio="1:1",
            scene_description="product on white background",
        )
        ref = content_svc.generate_media(tenant_id="tenant-a", brief=brief)
        self.assertTrue(ref.artifact_id)
        version = media_svc.get(tenant_id="tenant-a", version_id=ref.artifact_id)
        self.assertIsNotNone(version)
        self.assertEqual(ref.provider_id, "product_media")


class E2ETests(unittest.TestCase):
    def test_product_data_to_media_set(self):
        svc = _svc()
        v = svc.ingest(tenant_id="tenant-a", data=_png(1200, 1200))
        svc.link_product(
            tenant_id="tenant-a",
            version_id=v.version_id,
            product_id="SKU-100",
            sku="SKU-100",
            link_state=LINK_CONFIRMED,
        )
        report = svc.analyze(tenant_id="tenant-a", version_id=v.version_id, profile="website")
        media_set = svc.validate_set(
            tenant_id="tenant-a",
            product_id="SKU-100",
            items=[{"role": ROLE_HERO, "version_id": v.version_id}],
            profile="website",
        )
        self.assertIn("report", report)
        self.assertEqual(len(media_set.validation_errors), 0)

    def test_marketplace_variant_pipeline(self):
        svc = _svc()
        v1 = svc.ingest(tenant_id="tenant-a", data=_png(800, 800))
        v2 = svc.remove_background(tenant_id="tenant-a", version_id=v1.version_id)
        v3 = svc.transform_resize(tenant_id="tenant-a", version_id=v2.version_id, width=1000, height=1000, fit="pad")
        self.assertEqual(v3.parent_version_id, v2.version_id)
        self.assertNotEqual(v1.content_hash, v3.content_hash)

    def test_duplicate_catalog(self):
        svc = _svc()
        imgs = [_png(color=(i * 10, 20, 30)) for i in range(5)]
        imgs.append(imgs[0])
        versions = [svc.ingest(tenant_id="tenant-a", data=b) for b in imgs]
        dups = svc.find_duplicates(tenant_id="tenant-a", version_id=versions[0].version_id)
        self.assertTrue(any(v.version_id in dups for v in versions[1:]))

    def test_scale_similarity_bounded(self):
        svc = _svc()
        base = [_png(color=(i, 0, 0)) for i in range(20)]
        first = svc.ingest(tenant_id="tenant-a", data=base[0])
        for b in base[1:]:
            svc.ingest(tenant_id="tenant-a", data=b)
        results = svc.find_similar(tenant_id="tenant-a", version_id=first.version_id)
        self.assertLessEqual(len(results), 50)


if __name__ == "__main__":
    unittest.main()
