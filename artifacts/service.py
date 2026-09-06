"""Canonical artifact/file service facade (Block 3.5.1–3.5.6, 3.5.14, 3.5.17).

Single entry point used by:
- business_assistant_api (uploads, download/view endpoints, generated-file
  registration from tool execution results);
- business_assistant conversation gateway (trusted attachment resolution for
  agent/tool access, image-artifact registration for image.generate/edit).

Tenant isolation is enforced here, once, for every caller (3.5.14).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone

from artifacts.errors import ArtifactAccessDeniedError, ArtifactNotFoundError, ArtifactStorageFailedError
from artifacts.metrics import ARTIFACT_METRICS
from artifacts.models import (
    ArtifactRecord,
    KIND_IMAGE,
    SOURCE_EXTERNAL,
    SOURCE_GENERATED,
    SOURCE_UPLOAD,
    STATUS_ACTIVE,
    STATUS_DELETED,
    kind_for_mime,
)
from artifacts.store import ArtifactStoreBackend
from artifacts.validation import safe_filename, validate_artifact_upload

_PRODUCT_MEDIA_PREFIX = "product_media:"
_LEGACY_REF_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,256}$")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(content: bytes) -> str:
    return hashlib.sha256(content or b"").hexdigest()


def _failure_category_for(exc: Exception) -> str:
    from artifacts.errors import (
        ArtifactInvalidError,
        ArtifactStorageFailedError as _SF,
        ArtifactTooLargeError,
        ArtifactTypeNotAllowedError,
    )

    if isinstance(exc, ArtifactTooLargeError):
        return "too_large"
    if isinstance(exc, ArtifactTypeNotAllowedError):
        return "type_not_allowed"
    if isinstance(exc, ArtifactInvalidError):
        return "invalid"
    if isinstance(exc, _SF):
        return "storage_failed"
    return "unknown"


class ArtifactService:
    def __init__(self, *, store: ArtifactStoreBackend, media_provider=None, metrics=None):
        self.store = store
        self.media_provider = media_provider
        self.metrics = metrics or ARTIFACT_METRICS

    # --- registration (3.5.2, 3.5.5) ---

    def register_upload(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "",
        conversation_id: str = "",
        legacy_ref: str = "",
    ) -> ArtifactRecord:
        self.metrics.inc("artifact_upload_started", size_bytes=len(content or b""))
        try:
            safe_name, resolved_mime, kind = validate_artifact_upload(
                filename=filename, content=content, declared_mime=mime_type
            )
        except Exception as exc:
            self.metrics.inc("artifact_upload_failed", failure_category=_failure_category_for(exc))
            raise
        record = ArtifactRecord(
            artifact_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            owner_id=owner_id,
            filename=str(filename or safe_name),
            safe_filename=safe_name,
            mime_type=resolved_mime,
            size_bytes=len(content),
            kind=kind,
            created_at=_utc_iso(),
            storage_ref="sqlite:blob",
            source=SOURCE_UPLOAD,
            status=STATUS_ACTIVE,
            conversation_id=conversation_id,
            content_hash=_content_hash(content),
            legacy_ref=legacy_ref,
        )
        try:
            self.store.save(record, content)
        except Exception as exc:
            self.metrics.inc(
                "artifact_upload_failed", artifact_kind=kind, failure_category="storage_failed"
            )
            raise ArtifactStorageFailedError("artifact_persist_failed") from exc
        self.metrics.inc("artifact_upload_succeeded", artifact_kind=kind, size_bytes=len(content))
        self.metrics.inc("artifact_registered", artifact_kind=kind)
        return record

    def register_generated(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "",
        conversation_id: str = "",
        request_id: str = "",
        tool_id: str = "",
        derived_from_artifact_id: str = "",
    ) -> ArtifactRecord:
        safe_name, resolved_mime, kind = validate_artifact_upload(
            filename=filename, content=content, declared_mime=mime_type
        )
        record = ArtifactRecord(
            artifact_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            owner_id=owner_id,
            filename=str(filename or safe_name),
            safe_filename=safe_name,
            mime_type=resolved_mime,
            size_bytes=len(content),
            kind=kind,
            created_at=_utc_iso(),
            storage_ref="sqlite:blob",
            source=SOURCE_GENERATED,
            status=STATUS_ACTIVE,
            conversation_id=conversation_id,
            request_id=request_id,
            tool_id=tool_id,
            derived_from_artifact_id=derived_from_artifact_id,
            content_hash=_content_hash(content),
        )
        try:
            self.store.save(record, content)
        except Exception as exc:
            raise ArtifactStorageFailedError("artifact_persist_failed") from exc
        self.metrics.inc("artifact_generated", artifact_kind=kind, size_bytes=len(content))
        self.metrics.inc("artifact_registered", artifact_kind=kind)
        if derived_from_artifact_id:
            self.metrics.inc("artifact_transformation_completed", artifact_kind=kind)
        return record

    def register_external_image(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        version_id: str,
        mime_type: str = "image/png",
        filename: str = "",
        conversation_id: str = "",
        request_id: str = "",
        tool_id: str = "",
    ) -> ArtifactRecord:
        """Thin canonical wrapper over a product_media-owned image version.

        Bytes are never duplicated -- ``get_blob`` delegates to
        ``media_provider`` when ``storage_ref`` carries the product_media
        prefix. Existing image generation/persistence is not touched.
        """

        name = safe_filename(filename or f"image-{version_id}.png")
        record = ArtifactRecord(
            artifact_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            owner_id=owner_id,
            filename=name,
            safe_filename=name,
            mime_type=str(mime_type or "image/png"),
            size_bytes=0,
            kind=KIND_IMAGE,
            created_at=_utc_iso(),
            storage_ref=f"{_PRODUCT_MEDIA_PREFIX}{version_id}",
            source=SOURCE_EXTERNAL,
            status=STATUS_ACTIVE,
            conversation_id=conversation_id,
            request_id=request_id,
            tool_id=tool_id,
            metadata={"product_media_version_id": version_id},
        )
        self.store.save(record, None)
        self.metrics.inc("artifact_generated", artifact_kind=KIND_IMAGE)
        self.metrics.inc("artifact_registered", artifact_kind=KIND_IMAGE)
        return record

    # --- retrieval (3.5.6, 3.5.14) ---

    def _resolve_owned(self, *, tenant_id: str, ref: str) -> ArtifactRecord | None:
        """Resolve either a canonical artifact_id or a legacy
        ``artifact://upload/...`` ref to a tenant-owned record. Shared by
        metadata/blob retrieval and conversation-attach so every entry point
        accepts the same two reference forms consistently (3.5.6)."""

        rec = self.store.get(tenant_id=tenant_id, artifact_id=ref)
        if rec is not None:
            return rec
        return self.store.get_by_legacy_ref(tenant_id=tenant_id, ref=ref)

    def get_metadata(self, *, tenant_id: str, artifact_id: str) -> ArtifactRecord:
        rec = self._resolve_owned(tenant_id=tenant_id, ref=artifact_id)
        if rec is not None:
            if rec.status != STATUS_ACTIVE:
                # Block 3.5.13: a soft-deleted/missing artifact is gone from
                # every retrieval surface (metadata/view/download) even
                # though the row is retained for audit -- same fail-closed
                # shape as a genuine not-found, never a 500.
                self.metrics.inc("artifact_access_denied", artifact_kind=rec.kind, failure_category="not_found")
                raise ArtifactNotFoundError("artifact_not_found")
            return rec
        other = self.store.get_any_tenant(artifact_id)
        if other is not None:
            self.metrics.inc("artifact_access_denied", artifact_kind=other.kind, failure_category="access_denied")
            raise ArtifactAccessDeniedError("artifact_cross_tenant_denied")
        self.metrics.inc("artifact_access_denied", failure_category="not_found")
        raise ArtifactNotFoundError("artifact_not_found")

    def get_blob(self, *, tenant_id: str, artifact_id: str) -> tuple[ArtifactRecord, bytes]:
        rec = self.get_metadata(tenant_id=tenant_id, artifact_id=artifact_id)
        if rec.storage_ref.startswith(_PRODUCT_MEDIA_PREFIX):
            version_id = rec.storage_ref[len(_PRODUCT_MEDIA_PREFIX) :]
            if self.media_provider is None:
                raise ArtifactNotFoundError("artifact_backing_store_unavailable")
            blob = self.media_provider.get_blob(tenant_id=tenant_id, version_id=version_id)
            if blob is None:
                raise ArtifactNotFoundError("artifact_not_found")
            return rec, blob
        # Use the resolved canonical id (``rec.artifact_id``) here, not the
        # raw ``artifact_id`` argument -- the latter may be a legacy
        # ``artifact://upload/...`` ref that get_metadata() just resolved,
        # and the blob store is keyed by canonical id only.
        blob = self.store.get_blob(tenant_id=tenant_id, artifact_id=rec.artifact_id)
        if blob is None:
            raise ArtifactNotFoundError("artifact_not_found")
        return rec, blob

    def record_opened(self, rec: ArtifactRecord) -> None:
        self.metrics.inc("artifact_opened", artifact_kind=rec.kind)

    def record_downloaded(self, rec: ArtifactRecord) -> None:
        self.metrics.inc("artifact_downloaded", artifact_kind=rec.kind, size_bytes=rec.size_bytes)

    def record_download_failed(self, *, artifact_kind: str = "unknown", failure_category: str = "unknown") -> None:
        self.metrics.inc(
            "artifact_download_failed", artifact_kind=artifact_kind, failure_category=failure_category
        )

    # --- trusted agent/tool access (3.5.4) ---

    def resolve_trusted_ref(
        self, *, tenant_id: str, conversation_id: str, ref: str
    ) -> ArtifactRecord | None:
        """Resolve a user-attached reference (canonical artifact_id or legacy
        ``artifact://...`` URI) into a trusted, tenant-verified record.

        Never raises to the caller -- returns ``None`` (and emits
        ``artifact_access_denied`` telemetry for genuine spoof attempts) so a
        single bad/forged/foreign ref cannot break an entire tool call. This
        is the trust boundary: the caller only ever sees records that are
        provably owned by ``tenant_id``.
        """

        candidate = str(ref or "").strip()
        if not candidate or not _LEGACY_REF_RE.match(candidate):
            return None
        rec = self.store.get(tenant_id=tenant_id, artifact_id=candidate)
        if rec is None:
            rec = self.store.get_by_legacy_ref(tenant_id=tenant_id, ref=candidate)
        if rec is None:
            other = self.store.get_any_tenant(candidate)
            if other is not None:
                # Exists, but for a different tenant -- a real spoof attempt.
                self.metrics.inc(
                    "artifact_access_denied", artifact_kind=other.kind, failure_category="access_denied"
                )
            return None
        if rec.status != STATUS_ACTIVE:
            return None
        if rec.conversation_id and conversation_id and rec.conversation_id != conversation_id:
            self.metrics.inc(
                "artifact_access_denied", artifact_kind=rec.kind, failure_category="access_denied"
            )
            return None
        return rec

    def resolve_trusted_refs(
        self, *, tenant_id: str, conversation_id: str, refs: tuple[str, ...]
    ) -> list[dict]:
        out: list[dict] = []
        for ref in refs or ():
            rec = self.resolve_trusted_ref(tenant_id=tenant_id, conversation_id=conversation_id, ref=ref)
            if rec is None:
                continue
            out.append(
                {
                    "artifact_id": rec.artifact_id,
                    "filename": rec.safe_filename,
                    "mime_type": rec.mime_type,
                    "kind": rec.kind,
                }
            )
        return out

    # --- lifecycle (3.5.13) ---

    def delete_artifact(self, *, tenant_id: str, artifact_id: str) -> bool:
        """Soft-delete: row/metadata retained (audit trail), but every
        retrieval path (get_metadata/get_blob) starts fail-closed for it."""

        rec = self._resolve_owned(tenant_id=tenant_id, ref=artifact_id)
        if rec is None:
            return False
        ok = self.store.mark_status(tenant_id=tenant_id, artifact_id=rec.artifact_id, status=STATUS_DELETED)
        if ok:
            self.metrics.inc("artifact_deleted", artifact_kind=rec.kind)
        return ok

    def delete_artifacts_for_conversation(self, *, tenant_id: str, conversation_id: str) -> int:
        """Best-effort cascade when a conversation is deleted -- one failed
        artifact must never block deletion of the rest."""

        count = 0
        for rec in self.store.list_for_conversation(tenant_id=tenant_id, conversation_id=conversation_id):
            try:
                if self.delete_artifact(tenant_id=tenant_id, artifact_id=rec.artifact_id):
                    count += 1
            except Exception:
                continue
        return count

    def attach_to_conversation(
        self, *, tenant_id: str, artifact_id: str, conversation_id: str, message_id: str = ""
    ) -> None:
        """Best-effort: associate an already-registered artifact with the
        conversation/message it appears in (idempotent)."""

        rec = self._resolve_owned(tenant_id=tenant_id, ref=artifact_id)
        if rec is None or rec.conversation_id:
            return
        updated = ArtifactRecord(**{**rec.__dict__, "conversation_id": conversation_id, "message_id": message_id or rec.message_id})
        self.store.save(updated, None)


def kind_for(mime_type: str, filename: str = "") -> str:
    return kind_for_mime(mime_type, filename)
