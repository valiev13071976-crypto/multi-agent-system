"""Product Media Intelligence service facade (Block 10)."""

from __future__ import annotations

import io
import uuid
from dataclasses import asdict
from typing import Any

from PIL import Image

from product_media.access import assert_tenant_match, assert_version_access, sanitize_filename
from product_media.errors import (
    MEDIA_DELETED,
    MEDIA_GENERATION_FAILED,
    MEDIA_MISSING_SOURCE,
    MEDIA_NOT_FOUND,
    MEDIA_OUTPUT_INVALID,
    MEDIA_PRODUCT_LINK_AMBIGUOUS,
    MEDIA_RIGHTS_DENIED,
    MEDIA_RIGHTS_UNKNOWN,
    MEDIA_REVIEW_REQUIRED,
    MEDIA_TRANSFORM_FAILED,
    MEDIA_CANCELLED,
    MediaError,
)
from product_media.fingerprint import compute_dhash
from product_media.infographic import default_infographic_template, render_infographic
from product_media.observability import MediaObservability
from product_media.planner import assert_sync_media_allowed, assert_variant_limit, classify_media_workload
from product_media.platform_models import (
    GENERATION_PROFILE_VERSION,
    LINK_CANDIDATE,
    LINK_CONFIRMED,
    MAX_GENERATION_ATTEMPTS,
    MAX_QUALITY_RETRIES,
    MAX_VARIANTS_HARD,
    RIGHTS_GENERATED,
    RIGHTS_OWNED,
    RIGHTS_THIRD_PARTY_RESTRICTED,
    RIGHTS_UNKNOWN,
    ROLE_HERO,
    SOURCE_UPLOAD,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_REVIEW_REQUIRED,
    TRANSFORM_PROFILE_VERSION,
    MediaAssetVersion,
    MediaDeletionResult,
    MediaFingerprint,
    MediaJob,
    MediaRecipe,
    MediaRights,
    MediaSource,
    ProductMediaContext,
    ProductMediaLink,
    ProductMediaSet,
    VideoRecipe,
    content_hash_bytes,
)
from product_media.policy import MediaResourcePolicy
from product_media.profiles import get_target_profile, list_marketplace_profiles
from product_media.providers.fake import (
    FakeBackgroundRemovalProvider,
    FakeImageEditProvider,
    FakeImageGenerationProvider,
    FailingVariantProvider,
)
from product_media.quality import analyze_quality, quality_result_from_report
from product_media.recipes import build_recipe, recipe_from_brief, recipe_identity
from product_media.similarity import TenantSimilarityIndex
from product_media.store import MediaStore
from product_media.transform import (
    crop_image,
    enhance_image,
    replace_background as transform_replace_background,
    resize_image,
    strip_metadata,
    thumbnail_image,
)
from product_media.validation import validate_and_extract_image
from product_media.video import FakeVideoRenderer, build_video_recipe
from security.tenant import require_tenant_id


class ProductMediaService:
    def __init__(
        self,
        *,
        store: MediaStore,
        similarity_index: TenantSimilarityIndex | None = None,
        obs: MediaObservability | None = None,
        policy: MediaResourcePolicy | None = None,
        generator: FakeImageGenerationProvider | None = None,
        bg_remover: FakeBackgroundRemovalProvider | None = None,
        editor: FakeImageEditProvider | None = None,
    ):
        self.store = store
        self.similarity = similarity_index or TenantSimilarityIndex()
        self.obs = obs or MediaObservability()
        self.policy = policy or MediaResourcePolicy()
        self.generator = generator or FakeImageGenerationProvider()
        self.bg_remover = bg_remover or FakeBackgroundRemovalProvider()
        self.editor = editor or FakeImageEditProvider()
        self.video_renderer = FakeVideoRenderer()
        self._pending_transforms: dict[str, str] = {}
        self._recipe_cache: dict[str, str] = {}  # identity -> version_id
        self._rights: dict[str, MediaRights] = {}

    def ingest(
        self,
        *,
        tenant_id: str,
        data: bytes,
        filename: str = "",
        declared_mime: str = "",
        payload_tenant: str | None = None,
    ) -> MediaAssetVersion:
        tenant = assert_tenant_match(trusted=tenant_id, payload=payload_tenant)
        safe_name = sanitize_filename(filename)
        self.obs.emit("media.ingest.started", metadata={"filename": safe_name})
        validated = validate_and_extract_image(
            data, filename=safe_name, declared_mime=declared_mime, policy=self.policy
        )
        media_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        source_hash = content_hash_bytes(validated.data)
        chash = content_hash_bytes(validated.canonical_data)
        version = MediaAssetVersion(
            media_id=media_id,
            version_id=version_id,
            tenant_id=tenant,
            content_hash=chash,
            mime_type=validated.metadata.mime_type,
            media_type="image",
            byte_size=len(validated.canonical_data),
            width=validated.metadata.width,
            height=validated.metadata.height,
            status=STATUS_ACTIVE,
            operation="ingest",
            artifact_id=version_id,
            metadata_safe={
                "format": validated.metadata.format,
                "orientation": validated.metadata.orientation,
                "filename": safe_name,
                "source_kind": SOURCE_UPLOAD,
            },
            rights_status=RIGHTS_UNKNOWN,
            source_content_hash=source_hash,
        )
        # Immutable working original: orientation-normalized canonical bytes; raw hash preserved
        self.store.save_version(version, blob=validated.canonical_data)
        self._index_fingerprint(version, validated.canonical_data)
        self.obs.emit(
            "media.ingest.completed",
            metadata={"version_id": version_id, "hash": chash, "width": version.width, "height": version.height},
        )
        return version

    def get(self, *, tenant_id: str, version_id: str) -> MediaAssetVersion | None:
        tenant = require_tenant_id(tenant_id)
        version = self.store.get_version(version_id, tenant_id=tenant)
        if version is None:
            return None
        try:
            return assert_version_access(version, tenant_id=tenant)
        except MediaError:
            return None

    def analyze(self, *, tenant_id: str, version_id: str, profile: str = "website") -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        version = self.store.get_version(version_id, tenant_id=tenant)
        version = assert_version_access(version, tenant_id=tenant)
        blob = self.store.get_blob(version_id, tenant_id=tenant)
        if blob is None:
            raise MediaError(MEDIA_NOT_FOUND)
        self.obs.emit("media.analysis.started", metadata={"version_id": version_id})
        validated = validate_and_extract_image(blob, policy=self.policy)
        report = analyze_quality(
            tenant_id=tenant,
            version_id=version_id,
            metadata=validated.metadata,
            profile=profile,
        )
        self.obs.emit("media.quality.completed", metadata={"version_id": version_id, "issues": len(report.issues)})
        return {
            "report": {
                "report_id": report.report_id,
                "tenant_id": report.tenant_id,
                "version_id": report.version_id,
                "profile_version": report.profile_version,
                "issues": [{"code": i.code, "message": i.message, "severity": i.severity} for i in report.issues],
                "measurements": dict(report.measurements),
                "passed": report.passed,
            },
            "metadata": {
                "width": validated.metadata.width,
                "height": validated.metadata.height,
                "format": validated.metadata.format,
                "mime_type": validated.metadata.mime_type,
                "aspect_ratio": validated.metadata.aspect_ratio,
                "has_alpha": validated.metadata.has_alpha,
            },
        }

    def link_product(
        self,
        *,
        tenant_id: str,
        version_id: str,
        product_id: str,
        sku: str = "",
        link_state: str = LINK_CANDIDATE,
        source: str = "explicit",
    ) -> ProductMediaLink:
        tenant = require_tenant_id(tenant_id)
        version = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        if link_state == LINK_CONFIRMED and source == "visual_only":
            raise MediaError(MEDIA_PRODUCT_LINK_AMBIGUOUS, "visual inference cannot confirm product")
        link = ProductMediaLink(
            link_id=str(uuid.uuid4()),
            tenant_id=tenant,
            media_version_id=version.version_id,
            product_id=product_id,
            sku=sku,
            link_state=link_state,
            source=source,
        )
        self.store.save_link(link)
        return link

    def find_similar(self, *, tenant_id: str, version_id: str) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        version = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        blob = self.store.get_blob(version_id, tenant_id=tenant) or b""
        fp = MediaFingerprint(
            fingerprint_id=str(uuid.uuid4()),
            tenant_id=tenant,
            version_id=version_id,
            content_hash=version.content_hash,
            perceptual_hash=compute_dhash(blob),
        )
        results = self.similarity.find_similar(tenant_id=tenant, query=fp)
        self.obs.emit("media.similarity.completed", metadata={"version_id": version_id, "count": len(results)})
        return [asdict(r) for r in results]

    def find_duplicates(self, *, tenant_id: str, version_id: str) -> list[str]:
        tenant = require_tenant_id(tenant_id)
        version = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        return self.similarity.find_exact_duplicates(tenant_id=tenant, content_hash=version.content_hash)

    def transform_resize(
        self,
        *,
        tenant_id: str,
        version_id: str,
        width: int,
        height: int,
        fit: str = "contain",
    ) -> MediaAssetVersion:
        return self._transform(
            tenant_id=tenant_id,
            version_id=version_id,
            operation="resize",
            transform_fn=lambda b: resize_image(b, width=width, height=height, fit=fit),
        )

    def transform_crop(
        self,
        *,
        tenant_id: str,
        version_id: str,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> MediaAssetVersion:
        return self._transform(
            tenant_id=tenant_id,
            version_id=version_id,
            operation="crop",
            transform_fn=lambda b: crop_image(b, left=left, top=top, width=width, height=height),
        )

    def thumbnail(self, *, tenant_id: str, version_id: str, max_edge: int = 256) -> MediaAssetVersion:
        return self._transform(
            tenant_id=tenant_id,
            version_id=version_id,
            operation="thumbnail",
            transform_fn=lambda b: thumbnail_image(b, max_edge=max_edge),
        )

    def strip_metadata(self, *, tenant_id: str, version_id: str) -> MediaAssetVersion:
        return self._transform(
            tenant_id=tenant_id,
            version_id=version_id,
            operation="strip_metadata",
            transform_fn=lambda b: strip_metadata(b),
        )

    def remove_background(self, *, tenant_id: str, version_id: str) -> MediaAssetVersion:
        tenant = require_tenant_id(tenant_id)
        source = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        blob = self.store.get_blob(version_id, tenant_id=tenant)
        if blob is None:
            raise MediaError(MEDIA_NOT_FOUND)
        self.obs.emit("media.transform.started", metadata={"operation": "remove_background", "source": version_id})
        result = self.bg_remover.remove_background(source_bytes=blob)
        return self._save_derived(
            tenant=tenant,
            source=source,
            operation="remove_background",
            data=result.data,
            mime_type=result.mime_type,
            provider_id=result.provider_id,
        )

    def generate_from_brief(
        self,
        *,
        tenant_id: str,
        scene_description: str,
        aspect_ratio: str = "1:1",
        variant_count: int = 1,
        bulk: bool = False,
        media_brief_id: str = "",
    ) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        assert_variant_limit(variant_count, self.policy)
        assert_sync_media_allowed(variant_count=variant_count, bulk=bulk)
        width, height = self._parse_aspect(aspect_ratio)
        self.obs.emit("media.generation.started", metadata={"variants": variant_count, "brief_id": media_brief_id})
        versions: list[str] = []
        failures = 0
        for _ in range(variant_count):
            try:
                result = self.generator.generate(prompt=scene_description, width=width, height=height)
                validated = validate_and_extract_image(result.data, policy=self.policy)
                media_id = str(uuid.uuid4())
                version_id = str(uuid.uuid4())
                version = MediaAssetVersion(
                    media_id=media_id,
                    version_id=version_id,
                    tenant_id=tenant,
                    content_hash=content_hash_bytes(validated.canonical_data),
                    mime_type=validated.metadata.mime_type,
                    media_type="image",
                    byte_size=len(validated.canonical_data),
                    width=validated.metadata.width,
                    height=validated.metadata.height,
                    status=STATUS_ACTIVE,
                    operation="generate",
                    provider_id=result.provider_id,
                    transform_profile=GENERATION_PROFILE_VERSION,
                    artifact_id=version_id,
                    metadata_safe={"brief_id": media_brief_id, "prompt_excerpt": scene_description[:80]},
                )
                self.store.save_version(version, blob=validated.canonical_data)
                self._index_fingerprint(version, validated.canonical_data)
                versions.append(version_id)
            except MediaError:
                failures += 1
        status = "completed" if failures == 0 else "partial"
        self.obs.emit(
            "media.generation.completed",
            metadata={"generated": len(versions), "failed": failures, "status": status},
        )
        if not versions and failures:
            raise MediaError(MEDIA_GENERATION_FAILED)
        return {"version_ids": versions, "failed": failures, "status": status}

    def edit(
        self,
        *,
        tenant_id: str,
        source_version_id: str,
        instruction: str,
        mask_version_id: str | None = None,
    ) -> MediaAssetVersion:
        tenant = require_tenant_id(tenant_id)
        source = assert_version_access(
            self.store.get_version(source_version_id, tenant_id=tenant), tenant_id=tenant
        )
        blob = self.store.get_blob(source_version_id, tenant_id=tenant)
        if blob is None:
            raise MediaError(MEDIA_NOT_FOUND)
        mask_bytes = None
        if mask_version_id:
            mask_version = assert_version_access(
                self.store.get_version(mask_version_id, tenant_id=tenant), tenant_id=tenant
            )
            mask_bytes = self.store.get_blob(mask_version_id, tenant_id=tenant)
            if mask_bytes is None:
                raise MediaError(MEDIA_NOT_FOUND)
            with Image.open(io.BytesIO(blob)) as src, Image.open(io.BytesIO(mask_bytes)) as mask:
                if src.size != mask.size:
                    raise MediaError(MEDIA_OUTPUT_INVALID, "mask dimension mismatch")
        result = self.editor.edit(source_bytes=blob, instruction=instruction, mask_bytes=mask_bytes)
        validated = validate_and_extract_image(result.data, policy=self.policy)
        return self._save_derived(
            tenant=tenant,
            source=source,
            operation="edit",
            data=validated.canonical_data,
            mime_type=validated.metadata.mime_type,
            provider_id=result.provider_id,
        )

    def validate_set(
        self,
        *,
        tenant_id: str,
        product_id: str,
        items: list[dict[str, str]],
        profile: str = "marketplace",
    ) -> ProductMediaSet:
        tenant = require_tenant_id(tenant_id)
        errors: list[str] = []
        roles = {item["role"]: item["version_id"] for item in items}
        if ROLE_HERO not in roles:
            errors.append("missing_primary")
        seen_hashes: set[str] = set()
        for role, vid in roles.items():
            version = self.store.get_version(vid, tenant_id=tenant)
            try:
                version = assert_version_access(version, tenant_id=tenant)
            except MediaError:
                errors.append(f"invalid_version:{role}")
                continue
            if version.content_hash in seen_hashes:
                errors.append(f"duplicate_media:{role}")
            seen_hashes.add(version.content_hash)
            blob = self.store.get_blob(vid, tenant_id=tenant)
            if blob:
                validated = validate_and_extract_image(blob, policy=self.policy)
                report = analyze_quality(
                    tenant_id=tenant, version_id=vid, metadata=validated.metadata, profile=profile
                )
                if not report.passed:
                    errors.append(f"quality_failed:{role}")
        media_set = ProductMediaSet(
            set_id=str(uuid.uuid4()),
            tenant_id=tenant,
            product_id=product_id,
            items=tuple({"role": r, "version_id": v} for r, v in roles.items()),
            validation_errors=tuple(errors),
        )
        self.store.save_media_set(media_set)
        return media_set

    def analyze_video(
        self,
        *,
        tenant_id: str,
        data: bytes,
        duration_sec: float,
        width: int = 1280,
        height: int = 720,
        bulk: bool = False,
    ) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        assert_sync_media_allowed(video_duration_sec=duration_sec, bulk=bulk)
        max_frames = 30 if duration_sec <= 120 else 0
        if duration_sec > 120 and not bulk:
            from product_media.errors import MediaBatchRequired

            raise MediaBatchRequired()
        sampled = min(max_frames, max(1, int(duration_sec // 2)))
        self.obs.emit("media.video.analysis.started", metadata={"duration_sec": duration_sec, "frames": sampled})
        media_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        # Store minimal placeholder bytes for video identity contract
        placeholder = b"VIDEO_PLACEHOLDER"
        version = MediaAssetVersion(
            media_id=media_id,
            version_id=version_id,
            tenant_id=tenant,
            content_hash=content_hash_bytes(data or placeholder),
            mime_type="video/mp4",
            media_type="video",
            byte_size=len(data),
            width=width,
            height=height,
            status=STATUS_ACTIVE,
            operation="video_ingest",
            metadata_safe={"duration_sec": duration_sec, "sampled_frames": sampled},
        )
        self.store.save_version(version, blob=data or placeholder)
        poster = self.generator.generate(prompt="video_poster", width=320, height=180)
        poster_version = self._save_derived(
            tenant=tenant,
            source=version,
            operation="video_poster",
            data=poster.data,
            mime_type=poster.mime_type,
            provider_id=poster.provider_id,
        )
        self.obs.emit("media.video.analysis.completed", metadata={"version_id": version_id, "poster": poster_version.version_id})
        return {
            "version_id": version_id,
            "poster_version_id": poster_version.version_id,
            "sampled_frames": sampled,
            "duration_sec": duration_sec,
        }

    def delete(self, *, tenant_id: str, version_id: str) -> MediaDeletionResult:
        tenant = require_tenant_id(tenant_id)
        version = self.store.get_version(version_id, tenant_id=tenant)
        if version is None:
            raise MediaError(MEDIA_NOT_FOUND)
        if require_tenant_id(version.tenant_id) != tenant:
            from product_media.errors import MEDIA_CROSS_TENANT

            raise MediaError(MEDIA_CROSS_TENANT)
        removed = self.similarity.remove_version(tenant_id=tenant, version_id=version_id)
        self.store.tombstone_version(version_id, tenant_id=tenant)
        self.obs.emit("media.deleted", metadata={"version_id": version_id, "fingerprints_removed": removed})
        return MediaDeletionResult(
            deletion_id=str(uuid.uuid4()),
            tenant_id=tenant,
            version_id=version_id,
            status="tombstoned",
            fingerprints_removed=removed,
        )

    def queue_transform(self, *, tenant_id: str, version_id: str, operation: str) -> str:
        tenant = require_tenant_id(tenant_id)
        assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        job_id = str(uuid.uuid4())
        self._pending_transforms[job_id] = version_id
        self.store.save_job(
            MediaJob(job_id=job_id, tenant_id=tenant, operation=operation, status="queued", stage=operation)
        )
        return job_id

    def execute_pending_transform(self, *, tenant_id: str, job_id: str, operation: str = "thumbnail") -> dict:
        tenant = require_tenant_id(tenant_id)
        version_id = self._pending_transforms.get(job_id)
        if not version_id:
            job = self.store.get_job(job_id, tenant_id=tenant)
            if job is None:
                raise MediaError(MEDIA_NOT_FOUND)
            version_id = job_id  # fallback
        version = self.store.get_version(version_id, tenant_id=tenant)
        if version is None or version.status in ("tombstoned", "deleted"):
            self.obs.emit("media.failed", metadata={"job_id": job_id, "reason": "source_deleted"})
            raise MediaError(MEDIA_DELETED)
        if operation == "thumbnail":
            out = self.thumbnail(tenant_id=tenant, version_id=version_id)
            return {"status": "completed", "version_id": out.version_id}
        raise MediaError(MEDIA_TRANSFORM_FAILED)

    def bulk_ingest(
        self,
        *,
        tenant_id: str,
        items: list[bytes],
        resume_from: int = 0,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        tenant = require_tenant_id(tenant_id)
        workload = classify_media_workload(image_count=len(items))
        if workload == "sync" and len(items) > 1:
            assert_sync_media_allowed(image_count=len(items), bulk=True)
        jid = job_id or str(uuid.uuid4())
        completed: list[str] = []
        failed = 0
        checkpoint = resume_from
        batch_size = 100
        for idx in range(resume_from, len(items)):
            if idx - resume_from >= batch_size:
                break
            try:
                version = self.ingest(tenant_id=tenant, data=items[idx], filename=f"item_{idx}.png")
                completed.append(version.version_id)
            except MediaError:
                failed += 1
            checkpoint = idx + 1
        status = "partial" if failed else ("completed" if checkpoint >= len(items) else "running")
        self.store.save_job(
            MediaJob(
                job_id=jid,
                tenant_id=tenant,
                operation="bulk_ingest",
                status=status,
                checkpoint=checkpoint,
                total=len(items),
            )
        )
        return {"job_id": jid, "completed": len(completed), "failed": failed, "checkpoint": checkpoint, "version_ids": completed}

    def _transform(self, *, tenant_id, version_id, operation, transform_fn):
        tenant = require_tenant_id(tenant_id)
        source = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        blob = self.store.get_blob(version_id, tenant_id=tenant)
        if blob is None:
            raise MediaError(MEDIA_NOT_FOUND)
        self.obs.emit("media.transform.started", metadata={"operation": operation, "source": version_id})
        try:
            out_bytes, meta = transform_fn(blob)
            validated = validate_and_extract_image(out_bytes, policy=self.policy)
        except MediaError:
            raise
        except Exception as exc:
            raise MediaError(MEDIA_TRANSFORM_FAILED, str(exc)) from exc
        derived = self._save_derived(
            tenant=tenant,
            source=source,
            operation=operation,
            data=validated.canonical_data,
            mime_type=validated.metadata.mime_type,
            provider_id="local",
        )
        self.obs.emit("media.transform.completed", metadata={"operation": operation, "version_id": derived.version_id})
        return derived

    def _save_derived(
        self,
        *,
        tenant: str,
        source: MediaAssetVersion,
        operation: str,
        data: bytes,
        mime_type: str,
        provider_id: str,
        recipe_id: str = "",
        recipe_version: str = "",
        target_profile_id: str = "",
        rights_status: str | None = None,
        metadata_extra: dict | None = None,
    ) -> MediaAssetVersion:
        validated = validate_and_extract_image(data, policy=self.policy)
        version_id = str(uuid.uuid4())
        meta = dict(metadata_extra or {})
        version = MediaAssetVersion(
            media_id=source.media_id,
            version_id=version_id,
            tenant_id=tenant,
            content_hash=content_hash_bytes(validated.canonical_data),
            mime_type=mime_type,
            media_type="image",
            byte_size=len(validated.canonical_data),
            width=validated.metadata.width,
            height=validated.metadata.height,
            status=STATUS_ACTIVE,
            operation=operation,
            parent_version_id=source.version_id,
            transform_profile=TRANSFORM_PROFILE_VERSION,
            provider_id=provider_id,
            artifact_id=version_id,
            metadata_safe=meta,
            rights_status=rights_status if rights_status is not None else source.rights_status,
            source_content_hash=source.source_content_hash or source.content_hash,
            recipe_id=recipe_id,
            recipe_version=recipe_version,
            target_profile_id=target_profile_id,
        )
        self.store.save_version(version, blob=validated.canonical_data)
        self._index_fingerprint(version, validated.canonical_data)
        return version

    def set_rights(self, *, tenant_id: str, version_id: str, rights: MediaRights) -> MediaRights:
        tenant = require_tenant_id(tenant_id)
        assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        if rights.tenant_id != tenant:
            from product_media.errors import MEDIA_CROSS_TENANT

            raise MediaError(MEDIA_CROSS_TENANT)
        self._rights[version_id] = rights
        return rights

    def assert_export_rights(self, *, tenant_id: str, version_id: str, require_confirmed: bool = True) -> MediaRights:
        tenant = require_tenant_id(tenant_id)
        version = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        rights = self._rights.get(version_id) or MediaRights(
            rights_id="default",
            tenant_id=tenant,
            status=version.rights_status or RIGHTS_UNKNOWN,
        )
        if require_confirmed and rights.status in {RIGHTS_UNKNOWN, RIGHTS_THIRD_PARTY_RESTRICTED}:
            if rights.status == RIGHTS_THIRD_PARTY_RESTRICTED:
                raise MediaError(MEDIA_RIGHTS_DENIED)
            raise MediaError(MEDIA_RIGHTS_UNKNOWN)
        return rights

    def replace_background(
        self,
        *,
        tenant_id: str,
        version_id: str,
        mode: str = "solid",
        color: tuple[int, int, int] = (255, 255, 255),
    ) -> MediaAssetVersion:
        return self._transform(
            tenant_id=tenant_id,
            version_id=version_id,
            operation="background_replace",
            transform_fn=lambda b: transform_replace_background(b, mode=mode, color=color),
        )

    def enhance(
        self,
        *,
        tenant_id: str,
        version_id: str,
        strength: float = 1.0,
        generative: bool = False,
    ) -> MediaAssetVersion:
        tenant = require_tenant_id(tenant_id)
        source = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        blob = self.store.get_blob(version_id, tenant_id=tenant)
        if blob is None:
            raise MediaError(MEDIA_NOT_FOUND)
        out_bytes, meta = enhance_image(blob, strength=strength)
        status_rights = source.rights_status
        meta_safe = {"enhancement": "deterministic_local", "generative": False}
        if generative:
            meta_safe = {"enhancement": "generative", "generative": True, "fidelity_review_required": True}
            status_rights = RIGHTS_GENERATED
        derived = self._save_derived(
            tenant=tenant,
            source=source,
            operation="enhance",
            data=out_bytes,
            mime_type=meta.mime_type,
            provider_id="local",
            rights_status=status_rights,
            metadata_extra=meta_safe,
        )
        if generative:
            reviewed = MediaAssetVersion(
                media_id=derived.media_id,
                version_id=derived.version_id,
                tenant_id=derived.tenant_id,
                content_hash=derived.content_hash,
                mime_type=derived.mime_type,
                media_type=derived.media_type,
                byte_size=derived.byte_size,
                width=derived.width,
                height=derived.height,
                status=STATUS_REVIEW_REQUIRED,
                operation=derived.operation,
                parent_version_id=derived.parent_version_id,
                transform_profile=derived.transform_profile,
                provider_id=derived.provider_id,
                artifact_id=derived.artifact_id,
                created_at=derived.created_at,
                metadata_safe=dict(derived.metadata_safe),
                rights_status=derived.rights_status,
                source_content_hash=derived.source_content_hash,
                recipe_id=derived.recipe_id,
                recipe_version=derived.recipe_version,
                target_profile_id=derived.target_profile_id,
            )
            self.store.save_version(
                reviewed, blob=self.store.get_blob(derived.version_id, tenant_id=tenant) or out_bytes
            )
            return reviewed
        return derived

    def render_for_profile(
        self,
        *,
        tenant_id: str,
        version_id: str,
        profile_id: str,
    ) -> MediaAssetVersion:
        tenant = require_tenant_id(tenant_id)
        source = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        profile = get_target_profile(profile_id)
        recipe = build_recipe(
            tenant_id=tenant,
            operations=[
                {"name": "resize", "parameters": {"width": profile.width, "height": profile.height, "fit": "pad"}},
                {"name": "export", "parameters": {"format": profile.format}},
            ],
            target_profile_id=profile.profile_id,
        )
        return self.apply_recipe(tenant_id=tenant, version_id=source.version_id, recipe=recipe)

    def apply_recipe(
        self,
        *,
        tenant_id: str,
        version_id: str,
        recipe: MediaRecipe,
        use_cache: bool = True,
    ) -> MediaAssetVersion:
        tenant = require_tenant_id(tenant_id)
        source = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        identity = recipe_identity(source_hash=source.content_hash, recipe=recipe)
        if use_cache and identity in self._recipe_cache:
            cached = self.get(tenant_id=tenant, version_id=self._recipe_cache[identity])
            if cached is not None:
                return cached
        current_id = version_id
        current = source
        for op in recipe.operations:
            if op.name in {"resize", "pad", "export"}:
                params = dict(op.parameters)
                profile = get_target_profile(recipe.target_profile_id) if recipe.target_profile_id else None
                width = int(params.get("width") or (profile.width if profile else current.width))
                height = int(params.get("height") or (profile.height if profile else current.height))
                fit = str(params.get("fit") or "pad")
                current = self.transform_resize(
                    tenant_id=tenant, version_id=current_id, width=width, height=height, fit=fit
                )
                current_id = current.version_id
            elif op.name == "cleanup" or op.name == "strip_metadata" or op.name == "orientation_normalize":
                current = self.strip_metadata(tenant_id=tenant, version_id=current_id)
                current_id = current.version_id
            elif op.name == "background_remove":
                current = self.remove_background(tenant_id=tenant, version_id=current_id)
                current_id = current.version_id
            elif op.name == "background_replace":
                current = self.replace_background(tenant_id=tenant, version_id=current_id)
                current_id = current.version_id
            elif op.name == "enhance" or op.name == "sharpen":
                current = self.enhance(tenant_id=tenant, version_id=current_id)
                current_id = current.version_id
            elif op.name == "crop":
                params = dict(op.parameters)
                current = self.transform_crop(
                    tenant_id=tenant,
                    version_id=current_id,
                    left=int(params.get("left") or 0),
                    top=int(params.get("top") or 0),
                    width=int(params.get("width") or current.width),
                    height=int(params.get("height") or current.height),
                )
                current_id = current.version_id
            elif op.name in {"text_overlay", "infographic", "composite"}:
                continue  # handled by dedicated methods
            else:
                from product_media.errors import MEDIA_OPERATION_UNSUPPORTED

                raise MediaError(MEDIA_OPERATION_UNSUPPORTED, op.name)
        # Stamp recipe lineage on final derivative via re-save metadata path
        stamped = self._save_derived(
            tenant=tenant,
            source=current if current.parent_version_id else source,
            operation="recipe_apply",
            data=self.store.get_blob(current.version_id, tenant_id=tenant) or b"",
            mime_type=current.mime_type,
            provider_id="local",
            recipe_id=recipe.recipe_id,
            recipe_version=recipe.version,
            target_profile_id=recipe.target_profile_id,
        )
        self._recipe_cache[identity] = stamped.version_id
        return stamped

    def render_marketplace_set(
        self,
        *,
        tenant_id: str,
        version_id: str,
    ) -> dict[str, str]:
        """Same core → WB / Ozon / Yandex Market configurable profiles."""
        out: dict[str, str] = {}
        for profile in list_marketplace_profiles():
            derived = self.render_for_profile(
                tenant_id=tenant_id, version_id=version_id, profile_id=profile.profile_id
            )
            out[profile.profile_id] = derived.version_id
        return out

    def render_infographic(
        self,
        *,
        tenant_id: str,
        version_id: str,
        product_facts: dict[str, str],
        title: str = "",
        context: ProductMediaContext | None = None,
    ) -> MediaAssetVersion:
        tenant = require_tenant_id(tenant_id)
        source = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        blob = self.store.get_blob(version_id, tenant_id=tenant)
        if blob is None:
            raise MediaError(MEDIA_MISSING_SOURCE)
        facts = dict(product_facts)
        if context is not None:
            facts = {**dict(context.product_facts), **facts}
        template = default_infographic_template(tenant_id=tenant)
        rendered = render_infographic(
            product_image=blob, template=template, product_facts=facts, title=title
        )
        return self._save_derived(
            tenant=tenant,
            source=source,
            operation="infographic",
            data=rendered,
            mime_type="image/jpeg",
            provider_id="local",
            metadata_extra={"facts_used": sorted(facts.keys()), "template_id": template.template_id},
        )

    def render_banner(
        self,
        *,
        tenant_id: str,
        version_id: str,
        cta: str = "",
        profile_id: str = "banner_landscape",
    ) -> MediaAssetVersion:
        base = self.render_for_profile(tenant_id=tenant_id, version_id=version_id, profile_id=profile_id)
        if not cta:
            return base
        # Overlay CTA via edit provider (bounded text, not fact invention)
        return self.edit(tenant_id=tenant_id, source_version_id=base.version_id, instruction=f"CTA:{cta[:40]}")

    def create_video_recipe(
        self,
        *,
        tenant_id: str,
        scenes: list[dict],
        aspect_ratio: str = "9:16",
        duration_sec: float = 15.0,
        media_brief_id: str = "",
        rights_status: str = RIGHTS_OWNED,
    ) -> VideoRecipe:
        return build_video_recipe(
            tenant_id=tenant_id,
            scenes=scenes,
            aspect_ratio=aspect_ratio,
            duration_sec=duration_sec,
            media_brief_id=media_brief_id,
            rights_status=rights_status,
        )

    def render_video(self, *, tenant_id: str, recipe: VideoRecipe) -> MediaAssetVersion:
        tenant = require_tenant_id(tenant_id)
        if recipe.tenant_id != tenant:
            from product_media.errors import MEDIA_CROSS_TENANT

            raise MediaError(MEDIA_CROSS_TENANT)
        result = self.video_renderer.render(recipe=recipe)
        media_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        version = MediaAssetVersion(
            media_id=media_id,
            version_id=version_id,
            tenant_id=tenant,
            content_hash=str(result["content_hash"]),
            mime_type=str(result["mime_type"]),
            media_type="video",
            byte_size=len(result["data"]),
            width=int(result["width"]),
            height=int(result["height"]),
            status=STATUS_ACTIVE,
            operation="video_render",
            provider_id=str(result["provider_id"]),
            artifact_id=version_id,
            metadata_safe={
                "fake": bool(result.get("fake")),
                "recipe_id": recipe.recipe_id,
                "duration_sec": recipe.duration_sec,
                "media_brief_id": recipe.media_brief_id,
            },
            rights_status=str(result.get("rights_status") or recipe.rights_status),
            recipe_id=recipe.recipe_id,
            recipe_version=recipe.version,
            target_profile_id=recipe.target_profile_id,
        )
        self.store.save_version(version, blob=result["data"])
        return version

    def recipe_from_media_brief(
        self,
        *,
        tenant_id: str,
        aspect_ratio: str = "1:1",
        target_profile_id: str = "social_square",
    ) -> MediaRecipe:
        return recipe_from_brief(
            tenant_id=tenant_id,
            aspect_ratio=aspect_ratio,
            target_profile_id=target_profile_id,
        )

    def cancel_job(self, *, tenant_id: str, job_id: str) -> MediaJob:
        tenant = require_tenant_id(tenant_id)
        job = self.store.get_job(job_id, tenant_id=tenant)
        if job is None:
            raise MediaError(MEDIA_NOT_FOUND)
        cancelled = MediaJob(
            job_id=job.job_id,
            tenant_id=tenant,
            operation=job.operation,
            status=STATUS_CANCELLED,
            stage=job.stage,
            checkpoint=job.checkpoint,
            total=job.total,
            profile_version=job.profile_version,
        )
        self.store.save_job(cancelled)
        self.obs.emit("media.job.cancelled", metadata={"job_id": job_id, "checkpoint": job.checkpoint})
        return cancelled

    def bounded_generate(
        self,
        *,
        tenant_id: str,
        scene_description: str,
        max_attempts: int = MAX_GENERATION_ATTEMPTS,
        max_quality_retries: int = MAX_QUALITY_RETRIES,
        variant_count: int = 1,
        aspect_ratio: str = "1:1",
    ) -> dict:
        """No-loop generation: finite attempts then FAILED/REVIEW_REQUIRED."""
        attempts = 0
        quality_retries = 0
        last_error = ""
        while attempts < max_attempts and quality_retries <= max_quality_retries:
            attempts += 1
            try:
                result = self.generate_from_brief(
                    tenant_id=tenant_id,
                    scene_description=scene_description,
                    aspect_ratio=aspect_ratio,
                    variant_count=min(variant_count, MAX_VARIANTS_HARD),
                    bulk=True,
                )
                if result["version_ids"]:
                    return {
                        **result,
                        "attempts": attempts,
                        "quality_retries": quality_retries,
                        "terminated": True,
                    }
            except MediaError as exc:
                last_error = exc.code
                quality_retries += 1
        return {
            "version_ids": [],
            "failed": attempts,
            "status": "FAILED",
            "attempts": attempts,
            "quality_retries": quality_retries,
            "terminated": True,
            "error": last_error or MEDIA_GENERATION_FAILED,
            "review": MEDIA_REVIEW_REQUIRED,
        }

    def media_source_from_version(self, *, tenant_id: str, version_id: str) -> MediaSource:
        tenant = require_tenant_id(tenant_id)
        version = assert_version_access(self.store.get_version(version_id, tenant_id=tenant), tenant_id=tenant)
        return MediaSource(
            source_id=version.version_id,
            tenant_id=tenant,
            source_kind=str(version.metadata_safe.get("source_kind") or SOURCE_UPLOAD),
            media_type=version.media_type,
            mime=version.mime_type,
            content_hash=version.source_content_hash or version.content_hash,
            byte_size=version.byte_size,
            artifact_id=version.artifact_id,
            filename=str(version.metadata_safe.get("filename") or ""),
            rights_status=version.rights_status,
        )

    def _index_fingerprint(self, version: MediaAssetVersion, blob: bytes) -> None:
        fp = MediaFingerprint(
            fingerprint_id=str(uuid.uuid4()),
            tenant_id=version.tenant_id,
            version_id=version.version_id,
            content_hash=version.content_hash,
            perceptual_hash=compute_dhash(blob),
        )
        self.similarity.index(fp)

    @staticmethod
    def _parse_aspect(aspect_ratio: str) -> tuple[int, int]:
        if aspect_ratio == "1:1":
            return 512, 512
        if aspect_ratio == "16:9":
            return 640, 360
        return 512, 512
