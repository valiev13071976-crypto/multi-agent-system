"""Block 14 — ProductMedia package. Reuses product_media validation, checksum, transform."""

from __future__ import annotations

import hashlib
import uuid

from product_media.errors import MEDIA_CORRUPT, MEDIA_UNSUPPORTED, MediaError
from product_media.platform_models import content_hash_bytes
from product_media.transform import resize_image
from product_media.validation import validate_and_extract_image

from product_content.contracts import (
    AI_GENERATED_IMAGE,
    DEFAULT_ROLE_ORDER,
    DERIVED_IMAGE,
    MediaAssetRecord,
    MediaPackage,
    PROV_AI,
    PROV_DERIVED,
    PROV_FILE,
    ProductCard,
    ROLE_MAIN,
    ROLE_THUMBNAIL,
    SOURCE_IMAGE,
)
from product_content.errors import CONTENT_ACCESS_DENIED, ProductContentError


def _alt_text(card: ProductCard, role: str) -> str:
    bits = [card.canonical_title or card.product_name or card.sku, role.lower()]
    if card.color:
        bits.append(card.color)
    return " ".join(b for b in bits if b)[:180]


def ingest_media_assets(
    *,
    tenant_id: str,
    card: ProductCard,
    assets: list[dict],
    role_order: tuple[str, ...] = DEFAULT_ROLE_ORDER,
    make_thumbnail: bool = True,
) -> MediaPackage:
    if str(tenant_id) != str(card.tenant_id):
        raise ProductContentError(CONTENT_ACCESS_DENIED)
    records: list[MediaAssetRecord] = []
    originals: dict[str, bytes] = {}
    checksum_index: dict[str, list[str]] = {}
    issues: list[str] = []
    warnings: list[str] = []
    for raw in assets:
        asset_id = str(raw.get("asset_id") or uuid.uuid4())
        role = str(raw.get("role") or ROLE_MAIN).upper()
        kind = str(raw.get("kind") or SOURCE_IMAGE)
        data = raw.get("data")
        declared = str(raw.get("mime_type") or "")
        source = str(raw.get("source") or "fixture")
        if raw.get("url") and data is None:
            issues.append(f"{asset_id}:remote_url_rejected")
            records.append(
                MediaAssetRecord(
                    asset_id=asset_id,
                    product_id=card.product_id,
                    source=str(raw.get("url")),
                    source_type="remote_url_rejected",
                    mime_type=declared,
                    width=0,
                    height=0,
                    file_size=0,
                    checksum="",
                    role=role,
                    sort_order=0,
                    alt_text=_alt_text(card, role),
                    caption="",
                    provenance=PROV_FILE,
                    validation_status="INVALID",
                    warnings=("remote_url_rejected",),
                    kind=kind,
                    original_preserved=False,
                )
            )
            continue
        if kind == AI_GENERATED_IMAGE and data is None:
            records.append(
                MediaAssetRecord(
                    asset_id=asset_id,
                    product_id=card.product_id,
                    source=source,
                    source_type="fake_generated",
                    mime_type=declared or "image/png",
                    width=int(raw.get("width") or 0),
                    height=int(raw.get("height") or 0),
                    file_size=0,
                    checksum=hashlib.sha256(f"ai:{asset_id}".encode()).hexdigest(),
                    role=role,
                    sort_order=0,
                    alt_text=_alt_text(card, role),
                    caption=str(raw.get("caption") or ""),
                    provenance=PROV_AI,
                    validation_status="FIXTURE",
                    warnings=("ai_generated_not_a_source_fact",),
                    kind=AI_GENERATED_IMAGE,
                    original_preserved=True,
                )
            )
            continue
        if not isinstance(data, (bytes, bytearray)):
            issues.append(f"{asset_id}:missing_bytes")
            records.append(
                MediaAssetRecord(
                    asset_id=asset_id,
                    product_id=card.product_id,
                    source=source,
                    source_type=str(raw.get("source_type") or "unknown"),
                    mime_type=declared,
                    width=0,
                    height=0,
                    file_size=0,
                    checksum="",
                    role=role,
                    sort_order=0,
                    alt_text=_alt_text(card, role),
                    caption="",
                    provenance=PROV_FILE,
                    validation_status="INVALID",
                    warnings=("missing_bytes",),
                    kind=kind,
                    original_preserved=False,
                )
            )
            continue
        blob = bytes(data)
        if len(blob) == 0:
            issues.append(f"{asset_id}:zero_byte")
            records.append(
                MediaAssetRecord(
                    asset_id=asset_id,
                    product_id=card.product_id,
                    source=source,
                    source_type=str(raw.get("source_type") or "upload"),
                    mime_type=declared,
                    width=0,
                    height=0,
                    file_size=0,
                    checksum=content_hash_bytes(blob),
                    role=role,
                    sort_order=0,
                    alt_text=_alt_text(card, role),
                    caption="",
                    provenance=PROV_FILE,
                    validation_status="INVALID",
                    warnings=("zero_byte",),
                    kind=kind,
                    original_preserved=True,
                )
            )
            originals[asset_id] = blob
            continue
        try:
            validated = validate_and_extract_image(blob, filename=str(raw.get("filename") or ""), declared_mime=declared)
        except MediaError as exc:
            code = getattr(exc, "code", MEDIA_CORRUPT)
            if code == MEDIA_UNSUPPORTED:
                issues.append(f"{asset_id}:unsupported_mime")
                status = "UNSUPPORTED_MIME"
                warn = ("unsupported_mime",)
            else:
                issues.append(f"{asset_id}:corrupt")
                status = "CORRUPT"
                warn = ("corrupt",)
            records.append(
                MediaAssetRecord(
                    asset_id=asset_id,
                    product_id=card.product_id,
                    source=source,
                    source_type=str(raw.get("source_type") or "upload"),
                    mime_type=declared,
                    width=0,
                    height=0,
                    file_size=len(blob),
                    checksum=content_hash_bytes(blob),
                    role=role,
                    sort_order=0,
                    alt_text=_alt_text(card, role),
                    caption="",
                    provenance=PROV_FILE,
                    validation_status=status,
                    warnings=warn,
                    kind=kind,
                    original_preserved=True,
                )
            )
            originals[asset_id] = blob
            continue
        checksum = content_hash_bytes(blob)
        checksum_index.setdefault(checksum, []).append(asset_id)
        originals[asset_id] = blob  # original preserved — derived is separate
        rec = MediaAssetRecord(
            asset_id=asset_id,
            product_id=card.product_id,
            source=source,
            source_type=str(raw.get("source_type") or "upload"),
            mime_type=validated.metadata.mime_type,
            width=validated.metadata.width,
            height=validated.metadata.height,
            file_size=validated.metadata.byte_size,
            checksum=checksum,
            role=role,
            sort_order=0,
            alt_text=_alt_text(card, role),
            caption=str(raw.get("caption") or ""),
            provenance=PROV_FILE if kind == SOURCE_IMAGE else (PROV_AI if kind == AI_GENERATED_IMAGE else PROV_DERIVED),
            validation_status="VALID",
            warnings=(),
            kind=kind,
            original_preserved=True,
        )
        records.append(rec)
        if make_thumbnail and role == ROLE_MAIN and rec.validation_status == "VALID":
            thumb_bytes, thumb_meta = resize_image(blob, width=64, height=64, fit="contain")
            thumb_id = f"{asset_id}:thumb"
            originals[thumb_id] = blob  # pointer: original source still stored under parent
            records.append(
                MediaAssetRecord(
                    asset_id=thumb_id,
                    product_id=card.product_id,
                    source=asset_id,
                    source_type="derived_thumbnail",
                    mime_type=thumb_meta.mime_type,
                    width=thumb_meta.width,
                    height=thumb_meta.height,
                    file_size=thumb_meta.byte_size,
                    checksum=content_hash_bytes(thumb_bytes),
                    role=ROLE_THUMBNAIL,
                    sort_order=0,
                    alt_text=_alt_text(card, ROLE_THUMBNAIL),
                    caption="",
                    provenance=PROV_DERIVED,
                    validation_status="VALID",
                    warnings=(),
                    kind=DERIVED_IMAGE,
                    original_preserved=True,
                )
            )
            originals[f"{thumb_id}:derived"] = thumb_bytes

    order_index = {r: i for i, r in enumerate(role_order)}
    records.sort(key=lambda a: (order_index.get(a.role, 99), a.asset_id))
    ordered: list[MediaAssetRecord] = []
    for i, rec in enumerate(records):
        ordered.append(
            MediaAssetRecord(
                asset_id=rec.asset_id,
                product_id=rec.product_id,
                source=rec.source,
                source_type=rec.source_type,
                mime_type=rec.mime_type,
                width=rec.width,
                height=rec.height,
                file_size=rec.file_size,
                checksum=rec.checksum,
                role=rec.role,
                sort_order=i,
                alt_text=rec.alt_text,
                caption=rec.caption,
                provenance=rec.provenance,
                validation_status=rec.validation_status,
                warnings=rec.warnings,
                kind=rec.kind,
                original_preserved=rec.original_preserved,
            )
        )
    dupes = tuple(sorted(ch for ch, ids in checksum_index.items() if len(ids) > 1))
    if dupes:
        warnings.append("duplicate_checksum")
    return MediaPackage(
        assets=tuple(ordered),
        checksums=tuple(a.checksum for a in ordered if a.checksum),
        duplicate_checksums=dupes,
        original_bytes_by_asset=originals,
        warnings=tuple(warnings),
        issues=tuple(issues),
    )
