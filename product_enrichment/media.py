"""Product image acquisition, validation, dedup and Aspro-destination
processing (requirements 5-10).

Flow (requirement 6, mandatory): remote permitted URL -> governed binary
download (``media_fetch.GovernedImageFetcher``) -> validate actual image
content (``product_media.validation.validate_and_extract_image``) -> reject
obvious junk (requirement 7) -> dedup by content hash
(``product_media.platform_models.content_hash_bytes``) -> destination-
specific derivatives ONLY (preview/detail; gallery kept as extra accepted
candidates, never upscaled -- ``product_media.transform.resize_image``'s
"contain" fit calls ``Image.thumbnail``, which never enlarges) -> base64
Bitrix-ready payload. The external URL is retained on the result purely as
provenance (``MediaAsset.source_url``) -- it is NEVER the thing handed to
Bitrix; only ``filename``/``base64_content`` are (requirement 6).
"""

from __future__ import annotations

from typing import Sequence

from product_enrichment.media_fetch import ImageFetchPort, MediaFetchError
from product_enrichment.models import (
    MediaAsset,
    MediaCandidateInput,
    MediaResult,
    RejectedMediaCandidate,
    ResolvedIdentity,
)
from product_media.errors import MediaError
from product_media.platform_models import content_hash_bytes
from product_media.policy import MediaResourcePolicy
from product_media.transform import resize_image
from product_media.validation import validate_and_extract_image

# Destination-specific variants Bitrix/Aspro actually write today (#48's
# verified ``previewPicture``/``detailPicture`` -- see
# ``integrations.bitrix.schema.PREVIEW_PICTURE_FIELD``/``DETAIL_PICTURE_FIELD``).
# No fixed dimension is invented for "gallery" -- Aspro's multi-value
# gallery write shape is still unverified (deferred, same as #48's report),
# so extra accepted candidates are kept at their own validated/dedup'd
# size, tagged ``role="gallery"``, and simply never handed to the Bitrix
# write path.
_DESTINATION_DIMENSIONS: dict[str, tuple[int, int]] = {
    "preview": (300, 300),
    "detail": (1200, 1200),
}
_MIN_EDGE_PX = 100
_MIN_ASPECT_RATIO = 0.2
_MAX_ASPECT_RATIO = 5.0
_MAX_GALLERY_CANDIDATES = 4


def _reject(candidates: list[RejectedMediaCandidate], candidate: MediaCandidateInput, reason: str) -> None:
    candidates.append(RejectedMediaCandidate(source_url=candidate.url, source_type=candidate.source_type, reason=reason))


class MediaAcquisitionService:
    def __init__(self, *, fetcher: ImageFetchPort, policy: MediaResourcePolicy | None = None):
        self._fetcher = fetcher
        self._policy = policy or MediaResourcePolicy()

    async def acquire(
        self,
        candidates: Sequence[MediaCandidateInput],
        *,
        identity: ResolvedIdentity,
    ) -> MediaResult:
        rejected: list[RejectedMediaCandidate] = []
        seen_hashes: set[str] = set()
        accepted: list[tuple[bytes, MediaCandidateInput, str, object]] = []

        for candidate in candidates:
            try:
                raw = await self._fetcher.fetch_bytes(candidate.url)
            except MediaFetchError as exc:
                _reject(rejected, candidate, f"download_failed:{exc.code}")
                continue
            except Exception as exc:  # noqa: BLE001 -- never propagate a raw transport exception
                _reject(rejected, candidate, f"download_failed:{type(exc).__name__}")
                continue

            try:
                validated = validate_and_extract_image(raw, policy=self._policy)
            except MediaError as exc:
                _reject(rejected, candidate, f"invalid_image:{exc.code}")
                continue

            digest = content_hash_bytes(validated.canonical_data)
            if digest in seen_hashes:
                _reject(rejected, candidate, "duplicate_content_hash")
                continue

            meta = validated.metadata
            if meta.width < _MIN_EDGE_PX or meta.height < _MIN_EDGE_PX:
                _reject(rejected, candidate, "resolution_too_low")
                continue
            if not (_MIN_ASPECT_RATIO <= meta.aspect_ratio <= _MAX_ASPECT_RATIO):
                _reject(rejected, candidate, "aspect_ratio_out_of_range")
                continue

            seen_hashes.add(digest)
            accepted.append((validated.canonical_data, candidate, digest, meta))
            if len(accepted) >= 1 + _MAX_GALLERY_CANDIDATES:
                break

        if not accepted:
            return MediaResult(assets=(), rejected_candidates=tuple(rejected), status=MediaResult.STATUS_UNRESOLVED)

        assets: list[MediaAsset] = []
        master_data, master_candidate, master_digest, _master_meta = accepted[0]
        for role, (width, height) in _DESTINATION_DIMENSIONS.items():
            try:
                processed, meta = resize_image(master_data, width=width, height=height, fit="contain")
            except MediaError as exc:
                _reject(rejected, master_candidate, f"processing_failed:{exc.code}")
                continue
            assets.append(
                MediaAsset(
                    role=role,
                    content_hash=content_hash_bytes(processed),
                    filename=f"{identity.identity_key}_{role}.{meta.format}",
                    base64_content=_b64(processed),
                    width=meta.width,
                    height=meta.height,
                    mime_type=meta.mime_type,
                    source_url=master_candidate.url,
                    source_type=master_candidate.source_type,
                    processing=(f"resize_contain_{width}x{height}",),
                )
            )

        for data, candidate, digest, meta in accepted[1:]:
            assets.append(
                MediaAsset(
                    role="gallery",
                    content_hash=digest,
                    filename=f"{identity.identity_key}_gallery_{digest[:8]}.{meta.format}",
                    base64_content=_b64(data),
                    width=meta.width,
                    height=meta.height,
                    mime_type=meta.mime_type,
                    source_url=candidate.url,
                    source_type=candidate.source_type,
                    processing=(),
                )
            )

        has_preview_or_detail = any(a.role in _DESTINATION_DIMENSIONS for a in assets)
        status = MediaResult.STATUS_READY if has_preview_or_detail else MediaResult.STATUS_PARTIAL
        return MediaResult(assets=tuple(assets), rejected_candidates=tuple(rejected), status=status)


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")
