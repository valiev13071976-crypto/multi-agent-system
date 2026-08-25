"""Canonical MemoryService — business logic owner."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from autonomy.models import sanitize_metadata
from memory.access import MemoryAccessDenied, MemoryAccessPolicy, OP_DELETE, OP_READ, OP_UPDATE, OP_WRITE
from memory.context_builder import KnowledgeContextBuilder
from memory.models import (
    DEFAULT_MAX_RECORD_BYTES,
    LINK_SUPERSEDES,
    MEMORY_PROCEDURAL,
    MEMORY_SEMANTIC,
    MemoryIngestRequest,
    MemoryLink,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    content_hash_for_memory,
    normalize_memory_text,
    utc_now,
)
from memory.retention import MemoryRetentionPolicy
from memory.retrieval import MemoryRetriever
from memory.store import MemoryPersistenceUnavailableError, MemoryStore, MemoryVersionConflict
from memory.write_policy import MemoryWritePolicy
from security.encryption import (
    ENCRYPTION_REQUIRED,
    SENSITIVITY_SECRET,
    EncryptionService,
    EncryptionUnavailableError,
)
from security.redaction import redact


class MemoryDenied(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class MemoryRecordTooLarge(MemoryDenied):
    def __init__(self):
        super().__init__("memory_record_too_large")


class MemoryEncryptionUnavailable(MemoryDenied):
    def __init__(self):
        super().__init__("memory_encryption_unavailable")


_SECRET_MARKERS = (
    "GITHUB_WRITE_TOKEN",
    "PANDA_ENCRYPTION_KEY",
    "PANDA_CAPABILITY_SIGNING_KEY",
    "sk-",
    "ghp_",
    "ghs_",
    "Bearer ",
    "Authorization:",
)


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        access: MemoryAccessPolicy | None = None,
        retention: MemoryRetentionPolicy | None = None,
        retriever: MemoryRetriever | None = None,
        write_policy: MemoryWritePolicy | None = None,
        encryption: EncryptionService | None = None,
        context_builder: KnowledgeContextBuilder | None = None,
        observability=None,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        enabled: bool = True,
    ):
        self.store = store
        self.access = access or MemoryAccessPolicy()
        self.retention = retention or MemoryRetentionPolicy()
        self.encryption = encryption
        self.retriever = retriever or MemoryRetriever(encryption=encryption)
        self.write_policy = write_policy or MemoryWritePolicy()
        self.context_builder = context_builder or KnowledgeContextBuilder()
        self.observability = observability
        self.max_record_bytes = int(max_record_bytes)
        self.enabled = bool(enabled)
        self.blocked_reason: str | None = None

    def ingest(
        self,
        request: MemoryIngestRequest,
        *,
        requesting_scope=None,
        now: datetime | None = None,
        validated: bool = False,
        auto: bool = False,
    ) -> MemoryRecord:
        if not self.enabled or self.blocked_reason:
            raise MemoryDenied(self.blocked_reason or "memory_disabled")
        if not getattr(self.store, "available", True):
            raise MemoryPersistenceUnavailableError()
        stamp = now or utc_now()
        req_scope = requesting_scope or request.scope
        self._require_access(
            requesting=req_scope,
            target=request.scope,
            operation=OP_WRITE,
            memory_type=request.memory_type,
        )

        content = normalize_memory_text(request.content)
        if not content:
            raise MemoryDenied("memory_content_empty")
        if len(content.encode("utf-8")) > self.max_record_bytes:
            raise MemoryRecordTooLarge()

        poisoning = self.write_policy.is_poisoning_attempt(content)
        if auto and not self.write_policy.allow_auto_store(
            memory_type=request.memory_type,
            source_type=request.source_type,
            validated=validated,
            contains_policy_or_secret_instruction=poisoning,
        ):
            self._emit("memory.denied", status="denied", metadata={"reason": "auto_store_denied"})
            self._metric(
                "memory_denied_total",
                request.memory_type,
                request.sensitivity,
                scope_type=request.scope.scope_type,
                status="denied",
            )
            raise MemoryDenied("auto_store_denied")

        if request.sensitivity == SENSITIVITY_SECRET or self._looks_like_secret(content):
            # Prefer not storing secrets; deny durable secret memory by default.
            self._emit("memory.denied", status="denied", metadata={"reason": "secret_ingest_denied"})
            self._metric(
                "memory_denied_total",
                request.memory_type,
                "secret",
                scope_type=request.scope.scope_type,
                status="denied",
            )
            raise MemoryDenied("secret_ingest_denied")

        if poisoning and request.memory_type in {MEMORY_SEMANTIC, MEMORY_PROCEDURAL}:
            # Do not elevate poisoning text to trusted knowledge.
            if request.confidence is None or request.confidence > 0.2:
                request = MemoryIngestRequest(
                    scope=request.scope,
                    memory_type=request.memory_type,
                    content=content,
                    source_type=request.source_type,
                    source_id=request.source_id,
                    sensitivity=request.sensitivity,
                    confidence=0.1,
                    tags=request.tags + ("untrusted",),
                    title=request.title,
                    summary_safe=request.summary_safe,
                    created_by_component=request.created_by_component,
                    workflow_id=request.workflow_id,
                    task_id=request.task_id,
                    tool_id=request.tool_id,
                    external_reference=request.external_reference,
                    retention_ttl_seconds=request.retention_ttl_seconds,
                    metadata_safe={**dict(request.metadata_safe), "poisoning_flag": True},
                )

        sanitized = redact(content)
        digest = content_hash_for_memory(sanitized)

        existing = self.store.find_by_hash(request.scope, request.memory_type, digest)
        if existing is not None and existing.status == STATUS_ACTIVE:
            self._emit("memory.deduplicated", status="active", metadata={"memory_type": request.memory_type})
            self._metric(
                "memory_dedup_total",
                request.memory_type,
                request.sensitivity,
                scope_type=request.scope.scope_type,
            )
            return existing

        sensitivity = request.sensitivity
        content_safe = sanitized
        encrypted = None
        if sensitivity in ENCRYPTION_REQUIRED:
            if self.encryption is None:
                raise MemoryEncryptionUnavailable()
            try:
                encrypted = self.encryption.encrypt(sanitized).serialize()
            except EncryptionUnavailableError as exc:
                raise MemoryEncryptionUnavailable() from exc
            content_safe = None

        expires = self.retention.expires_at(
            memory_type=request.memory_type,
            sensitivity=sensitivity,
            scope_type=request.scope.scope_type,
            now=stamp,
            override_ttl_seconds=request.retention_ttl_seconds,
        )
        provenance = MemoryProvenance(
            source_type=request.source_type,
            source_id=request.source_id,
            created_by_component=request.created_by_component,
            ingested_at=stamp,
            source_hash=digest,
            workflow_id=request.workflow_id,
            task_id=request.task_id,
            tool_id=request.tool_id,
            external_reference=request.external_reference,
        )
        # Semantic/procedural require provenance (always present here).
        record = MemoryRecord(
            memory_id=str(uuid.uuid4()),
            memory_type=request.memory_type,
            scope=request.scope,
            content_hash=digest,
            source_type=request.source_type,
            source_ref=request.source_id,
            provenance=provenance,
            sensitivity=sensitivity,
            status=STATUS_ACTIVE,
            created_at=stamp,
            updated_at=stamp,
            title=request.title,
            content_safe=content_safe,
            encrypted_content=encrypted,
            summary_safe=request.summary_safe,
            confidence=request.confidence,
            tags=request.tags,
            expires_at=expires,
            metadata_safe=dict(request.metadata_safe),
        )
        created = self.store.create(record, provenance, tags=request.tags)
        if created.memory_id == record.memory_id:
            self._emit("memory.ingested", status="active", metadata={"memory_type": record.memory_type})
            self._metric(
                "memory_ingest_total",
                record.memory_type,
                record.sensitivity,
                scope_type=record.scope.scope_type,
            )
        else:
            self._emit("memory.deduplicated", status="active", metadata={"memory_type": created.memory_type})
            self._metric(
                "memory_dedup_total",
                record.memory_type,
                record.sensitivity,
                scope_type=record.scope.scope_type,
            )
        return created

    def retrieve(self, query: MemoryQuery, *, requesting_scope=None) -> tuple:
        if not self.enabled or self.blocked_reason:
            raise MemoryDenied(self.blocked_reason or "memory_disabled")
        req = requesting_scope or query.scope
        mem_type = ",".join(query.memory_types) if query.memory_types else "any"
        self._require_access(
            requesting=req,
            target=query.scope,
            operation=OP_READ,
            memory_type=mem_type,
        )
        started = utc_now()
        results = self.retriever.search(self.store, query)
        elapsed_ms = max(0, int((utc_now() - started).total_seconds() * 1000))
        self._emit(
            "memory.retrieved",
            status="ok",
            metadata={"count": len(results), "memory_type": ",".join(query.memory_types) or "any"},
        )
        self._metric(
            "memory_retrieval_total",
            "any",
            "internal",
            scope_type=query.scope.scope_type,
        )
        obs = self.observability
        if obs is not None and getattr(obs, "metrics", None) is not None:
            try:
                obs.metrics.observe_latency(
                    "memory_retrieval_latency_ms",
                    float(elapsed_ms),
                    labels={
                        "component": "memory",
                        "memory_type": "any",
                        "sensitivity": "internal",
                        "status": "ok",
                        "scope_type": query.scope.scope_type,
                    },
                )
            except Exception:
                pass
        return results

    def supersede(
        self,
        old_memory_id: str,
        new_request: MemoryIngestRequest,
        *,
        requesting_scope=None,
        now: datetime | None = None,
    ) -> MemoryRecord:
        stamp = now or utc_now()
        old = self.store.get(old_memory_id)
        if old is None:
            raise MemoryVersionConflict("memory_not_found")
        req = requesting_scope or new_request.scope
        self._require_access(
            requesting=req,
            target=old.scope,
            operation=OP_UPDATE,
            memory_type=old.memory_type,
        )
        self._require_access(
            requesting=req,
            target=new_request.scope,
            operation=OP_WRITE,
            memory_type=new_request.memory_type,
        )
        new_record = self.ingest(new_request, requesting_scope=req, now=stamp)
        from memory.store import _clone

        superseded = _clone(
            old,
            status=STATUS_SUPERSEDED,
            updated_at=stamp,
        )
        self.store.update(superseded, expected_version=old.version)
        self.store.link(
            MemoryLink(
                link_id=str(uuid.uuid4()),
                from_memory_id=new_record.memory_id,
                to_memory_id=old.memory_id,
                link_type=LINK_SUPERSEDES,
                created_at=stamp,
            )
        )
        self._emit("memory.superseded", status="superseded", metadata={"old": old_memory_id})
        return new_record

    def forget(
        self,
        memory_id: str,
        *,
        requesting_scope,
        reason: str = "forget",
        now: datetime | None = None,
    ) -> MemoryRecord:
        _ = now
        row = self.store.get(memory_id)
        if row is None:
            # Idempotent: fabricate tombstone-like denial avoidance
            raise MemoryVersionConflict("memory_not_found")
        if row.status == "deleted":
            return row
        self._require_access(
            requesting=requesting_scope,
            target=row.scope,
            operation=OP_DELETE,
            memory_type=row.memory_type,
        )
        deleted = self.store.delete(memory_id, expected_version=row.version)
        self._emit("memory.forgotten", status="deleted", metadata={"reason": reason})
        self._metric(
            "memory_forget_total",
            row.memory_type,
            row.sensitivity,
            scope_type=row.scope.scope_type,
        )
        return deleted

    def get(self, memory_id: str, *, requesting_scope) -> MemoryRecord | None:
        row = self.store.get(memory_id)
        if row is None or row.status == "deleted":
            return None
        try:
            self.access.require(
                requesting=requesting_scope, target=row.scope, operation=OP_READ
            )
        except MemoryAccessDenied:
            # Silent deny: do not emit (avoids presence leak via observability).
            return None
        return row

    def build_context(self, results) -> object:
        return self.context_builder.build(results)

    def _require_access(
        self,
        *,
        requesting,
        target,
        operation: str,
        memory_type: str | None = None,
    ) -> None:
        try:
            self.access.require(
                requesting=requesting, target=target, operation=operation
            )
        except MemoryAccessDenied:
            meta = {
                "reason_code": "memory_scope_access_denied",
                "operation": operation,
                "scope_type": requesting.scope_type,
                "target_scope_type": target.scope_type,
            }
            if memory_type:
                meta["memory_type"] = memory_type
            self._emit("memory.denied", status="denied", metadata=meta)
            self._metric(
                "memory_denied_total",
                memory_type or "unknown",
                "internal",
                scope_type=requesting.scope_type,
                status="denied",
            )
            raise

    def _looks_like_secret(self, content: str) -> bool:
        if any(m in content for m in _SECRET_MARKERS):
            return True
        if re.search(r"(?i)\b(api[_-]?key|password|secret)\b\s*[:=]", content):
            return True
        return False

    def _emit(self, event_type: str, *, status: str = "", metadata: dict | None = None) -> None:
        obs = self.observability
        if obs is None:
            return
        try:
            # Never put raw content/query/scope_id in events.
            safe_meta = sanitize_metadata(
                {k: v for k, v in dict(metadata or {}).items() if k not in {"content", "query", "scope_id"}}
            )
            obs.emit(event_type, component="memory", status=status, metadata=safe_meta)
        except Exception:
            pass

    def _metric(
        self,
        name: str,
        memory_type: str,
        sensitivity: str,
        *,
        scope_type: str = "n_a",
        status: str = "ok",
    ) -> None:
        obs = self.observability
        if obs is None or not getattr(obs, "metrics", None):
            return
        try:
            obs.metrics.inc(
                name,
                labels={
                    "component": "memory",
                    "memory_type": memory_type or "unknown",
                    "sensitivity": sensitivity or "unknown",
                    "status": status,
                    "scope_type": scope_type or "n_a",
                },
            )
        except Exception:
            pass
