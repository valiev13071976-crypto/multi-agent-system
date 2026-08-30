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
    MEDIA_NOT_FOUND,
    MEDIA_OUTPUT_INVALID,
    MEDIA_PRODUCT_LINK_AMBIGUOUS,
    MEDIA_TRANSFORM_FAILED,
    MediaError,
)
from product_media.fingerprint import compute_dhash
from product_media.observability import MediaObservability
from product_media.planner import assert_sync_media_allowed, assert_variant_limit, classify_media_workload
from product_media.platform_models import (
    GENERATION_PROFILE_VERSION,
    LINK_CANDIDATE,
    LINK_CONFIRMED,
    ROLE_HERO,
    STATUS_ACTIVE,
    TRANSFORM_PROFILE_VERSION,
    MediaAssetVersion,
    MediaDeletionResult,
    MediaFingerprint,
    MediaJob,
    ProductMediaLink,
    ProductMediaSet,
    content_hash_bytes,
)
from product_media.policy import MediaResourcePolicy
from product_media.providers.fake import (
    FakeBackgroundRemovalProvider,
    FakeImageEditProvider,
    FakeImageGenerationProvider,
    FailingVariantProvider,
)
from product_media.quality import analyze_quality
from product_media.similarity import TenantSimilarityIndex
from product_media.store import MediaStore
from product_media.transform import crop_image, resize_image, strip_metadata, thumbnail_image
from product_media.validation import validate_and_extract_image
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
        self._pending_transforms: dict[str, str] = {}

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
            },
        )
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
    ) -> MediaAssetVersion:
        validated = validate_and_extract_image(data, policy=self.policy)
        version_id = str(uuid.uuid4())
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
        )
        self.store.save_version(version, blob=validated.canonical_data)
        self._index_fingerprint(version, validated.canonical_data)
        return version

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
