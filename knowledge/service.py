"""Canonical KnowledgeService — external knowledge / RAG coordinator."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime

from autonomy.models import sanitize_metadata
from knowledge.access import OP_INGEST, OP_READ, OP_REFRESH, KnowledgeAccessDenied, KnowledgeAccessPolicy
from knowledge.freshness import expires_at_for, freshness_label, is_stale
from knowledge.models import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_ITEM_BYTES,
    DEFAULT_MAX_RESULTS,
    FRESHNESS_TTL,
    STATUS_ACTIVE,
    TRUST_RANK,
    TRUST_UNVERIFIED,
    FreshnessPolicy,
    KnowledgeIngestRequest,
    KnowledgeItem,
    KnowledgeProvenance,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeSource,
    citation_ref_for,
    content_hash_text,
    normalize_knowledge_text,
)
from knowledge.ranking import detect_conflicts, merge_and_rank
from knowledge.rag_context import RAGContextBuilder
from knowledge.registry import KnowledgeSourceRegistry, KnowledgeSourceRegistryError
from knowledge.validator import KnowledgeValidationError, KnowledgeValidator
from knowledge.write_policy import KnowledgeWritePolicy
from memory.models import (
    MEMORY_SEMANTIC,
    MEMORY_WORKING_REFERENCE,
    SOURCE_DOCUMENT,
    SOURCE_EXTERNAL,
    SOURCE_OPERATOR,
    SOURCE_SYSTEM,
    MemoryIngestRequest,
    MemoryScope,
    utc_now,
)
from security.encryption import SENSITIVITY_INTERNAL, SENSITIVITY_SECRET
from security.redaction import redact
from tools.url_safety import UnsafeUrlError, validate_http_url


_SECRET_MARKERS = (
    "GITHUB_WRITE_TOKEN",
    "PANDA_ENCRYPTION_KEY",
    "sk-",
    "ghp_",
    "Bearer ",
    "Authorization:",
)
_POISON = (
    "ignore previous instructions",
    "ignore all previous",
    "enable privileged tools",
    "bypass autonomy",
    "disable hitl",
)


class KnowledgeDenied(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class KnowledgeService:
    def __init__(
        self,
        registry: KnowledgeSourceRegistry,
        *,
        access: KnowledgeAccessPolicy | None = None,
        validator: KnowledgeValidator | None = None,
        write_policy: KnowledgeWritePolicy | None = None,
        rag_builder: RAGContextBuilder | None = None,
        memory_service=None,
        document_service=None,
        tool_gateway=None,
        observability=None,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
        enabled: bool = True,
    ):
        self.registry = registry
        self.access = access or KnowledgeAccessPolicy()
        self.validator = validator or KnowledgeValidator()
        self.write_policy = write_policy or KnowledgeWritePolicy()
        self.rag_builder = rag_builder or RAGContextBuilder()
        self.memory_service = memory_service
        self.document_service = document_service
        self.tool_gateway = tool_gateway
        self.observability = observability
        self.max_item_bytes = int(max_item_bytes)
        self.max_results = int(max_results)
        self.max_context_bytes = int(max_context_bytes)
        self.enabled = bool(enabled)
        self.blocked_reason: str | None = None
        self._items: dict[str, KnowledgeItem] = {}
        self._cache: dict[str, tuple[float, tuple[KnowledgeResult, ...]]] = {}
        self._cache_ttl_seconds = 30.0

    def register_source(self, source: KnowledgeSource, adapter=None) -> KnowledgeSource:
        if self.registry.frozen:
            raise KnowledgeSourceRegistryError("registry_frozen")
        # SSRF check for any URL metadata on external sources
        url = str(source.metadata_safe.get("url") or "")
        if url:
            try:
                validate_http_url(url)
            except UnsafeUrlError as exc:
                raise KnowledgeDenied(f"ssrf_denied:{exc.reason}") from exc
        registered = self.registry.register(source, adapter=adapter)
        self._emit(
            "knowledge.source_registered",
            status="ok",
            metadata={"source_type": source.source_type, "trust_level": source.trust_level},
        )
        return registered

    def ingest(
        self,
        request: KnowledgeIngestRequest,
        *,
        requesting_scope: MemoryScope | None = None,
        now: datetime | None = None,
    ) -> KnowledgeItem:
        if not self.enabled or self.blocked_reason:
            raise KnowledgeDenied(self.blocked_reason or "knowledge_disabled")
        stamp = now or utc_now()
        req_scope = requesting_scope or request.scope
        self._require_access(req_scope, request.scope, OP_INGEST)

        source = self.registry.get(request.source_id)
        if source.scope.key() != request.scope.key():
            raise KnowledgeAccessDenied("cross_scope_denied")
        if not source.enabled:
            raise KnowledgeDenied("source_disabled")

        try:
            self.validator.validate_ingest(request, max_bytes=self.max_item_bytes)
        except KnowledgeValidationError as exc:
            raise KnowledgeDenied(exc.reason) from exc

        content = normalize_knowledge_text(request.content)
        if self._looks_like_secret(content):
            self._emit("knowledge.denied", status="denied", metadata={"reason": "secret_denied"})
            self._metric("knowledge_denied_total", source.source_type, "denied", request.trust_level)
            raise KnowledgeDenied("secret_denied")
        poisoning = self._looks_like_poison(content)
        trust = request.trust_level
        if poisoning and trust != TRUST_UNVERIFIED:
            trust = TRUST_UNVERIFIED

        if not self.write_policy.allow_persist(
            trust_level=trust,
            validated=request.validated,
            contains_secret=False,
            contains_policy_instruction=poisoning,
        ):
            self._emit("knowledge.denied", status="denied", metadata={"reason": "write_policy_denied"})
            self._metric("knowledge_denied_total", source.source_type, "denied", trust)
            raise KnowledgeDenied("write_policy_denied")

        # Unverified cannot be elevated
        if trust == TRUST_UNVERIFIED and request.validated is False:
            # still may persist as low-trust if write_policy allowed via validated path only
            pass

        digest = content_hash_text(content)
        # Dedup within scope+source
        for existing in self._items.values():
            if (
                existing.scope.key() == request.scope.key()
                and existing.source_id == request.source_id
                and existing.content_hash == digest
                and existing.status == STATUS_ACTIVE
            ):
                self._emit("knowledge.ingested", status="dedup", metadata={"source_type": source.source_type})
                return existing

        freshness = request.freshness or source.refresh_policy or FreshnessPolicy()
        expires = expires_at_for(freshness, now=stamp)
        kid = str(uuid.uuid4())
        provenance = KnowledgeProvenance(
            source_id=request.source_id,
            source_type=source.source_type,
            source_ref=request.provenance_source_ref,
            ingested_at=stamp,
            source_hash=digest,
            trust_level=trust,
            validation_state="validated" if request.validated else "unvalidated",
            document_id=request.document_id,
            chunk_id=request.chunk_id,
            tool_id=request.tool_id,
            external_reference=request.external_reference,
            retrieved_at=stamp,
        )
        item = KnowledgeItem(
            knowledge_id=kid,
            scope=request.scope,
            source_id=request.source_id,
            content=redact(content),
            content_hash=digest,
            trust_level=trust,
            provenance=provenance,
            sensitivity=request.sensitivity,
            status=STATUS_ACTIVE,
            created_at=stamp,
            updated_at=stamp,
            confidence=request.confidence,
            freshness=freshness.policy,
            expires_at=expires,
            metadata_safe={
                **dict(request.metadata_safe),
                "tags": list(request.tags),
                "allow_stale": bool(freshness.allow_stale),
            },
        )
        self.validator.validate_item(item)

        memory_id = None
        if request.persist and self.memory_service is not None:
            mem_type = (
                MEMORY_SEMANTIC
                if trust in {"system_trusted", "operator_trusted", "validated_internal", "document_sourced"}
                else MEMORY_WORKING_REFERENCE
            )
            mem_source = SOURCE_EXTERNAL
            if trust in {"operator_trusted"}:
                mem_source = SOURCE_OPERATOR
            elif trust in {"system_trusted"}:
                mem_source = SOURCE_SYSTEM
            elif source.source_type == "document":
                mem_source = SOURCE_DOCUMENT
            try:
                record = self.memory_service.ingest(
                    MemoryIngestRequest(
                        scope=request.scope,
                        memory_type=mem_type,
                        content=item.content,
                        source_type=mem_source,
                        source_id=request.source_id,
                        sensitivity=request.sensitivity
                        if request.sensitivity != SENSITIVITY_SECRET
                        else SENSITIVITY_INTERNAL,
                        confidence=request.confidence if request.confidence is not None else 0.5,
                        tags=tuple(request.tags) + ("knowledge",),
                        created_by_component="knowledge_service",
                        external_reference=kid,
                        metadata_safe={
                            "knowledge_id": kid,
                            "trust_level": trust,
                            "citation_ref": item.citation_ref,
                            "source_type": source.source_type,
                            "freshness": freshness.policy,
                        },
                    ),
                    requesting_scope=req_scope,
                    validated=request.validated,
                    auto=False,
                )
                memory_id = record.memory_id
            except Exception:
                memory_id = None

        if memory_id:
            item = KnowledgeItem(
                knowledge_id=item.knowledge_id,
                scope=item.scope,
                source_id=item.source_id,
                content=item.content,
                content_hash=item.content_hash,
                trust_level=item.trust_level,
                provenance=item.provenance,
                sensitivity=item.sensitivity,
                status=item.status,
                created_at=item.created_at,
                updated_at=item.updated_at,
                summary_safe=item.summary_safe,
                confidence=item.confidence,
                freshness=item.freshness,
                expires_at=item.expires_at,
                version=item.version,
                metadata_safe=dict(item.metadata_safe),
                memory_id=memory_id,
            )
        self._items[item.knowledge_id] = item
        self._cache.clear()
        self._emit(
            "knowledge.ingested",
            status="active",
            metadata={"source_type": source.source_type, "trust_level": trust},
        )
        self._metric("knowledge_ingest_total", source.source_type, "active", trust)
        return item

    def retrieve(
        self,
        query: KnowledgeQuery,
        *,
        requesting_scope: MemoryScope | None = None,
        now: datetime | None = None,
    ) -> tuple[KnowledgeResult, ...]:
        if not self.enabled or self.blocked_reason:
            raise KnowledgeDenied(self.blocked_reason or "knowledge_disabled")
        stamp = now or utc_now()
        req = requesting_scope or query.scope
        self._require_access(req, query.scope, OP_READ)

        cache_key = self._cache_key(query)
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, rows = cached
            if (stamp.timestamp() - ts) <= self._cache_ttl_seconds:
                return rows

        started = utc_now()
        sources = self.registry.list_sources(enabled_only=True)
        if query.source_ids:
            wanted = set(query.source_ids)
            sources = tuple(s for s in sources if s.source_id in wanted)
        if query.source_types:
            types = set(query.source_types)
            sources = tuple(s for s in sources if s.source_type in types)

        collected: list[KnowledgeResult] = []
        # Local ingested items
        for item in self._items.values():
            if item.scope.key() != query.scope.key() or item.status != STATUS_ACTIVE:
                continue
            src = None
            try:
                src = self.registry.get(item.source_id)
            except KnowledgeSourceRegistryError:
                continue
            if not src.enabled:
                continue
            if query.source_ids and item.source_id not in query.source_ids:
                continue
            stale = is_stale(expires_at=item.expires_at, policy=src.refresh_policy, now=stamp)
            allow_stale = bool(src.refresh_policy.allow_stale) or bool(
                (item.metadata_safe or {}).get("allow_stale")
            )
            if stale and query.freshness_required and not query.include_stale:
                self._metric("knowledge_stale_result_total", src.source_type, "excluded", item.trust_level)
                continue
            if stale and not query.include_stale and not allow_stale:
                continue
            if query.trust_min and TRUST_RANK.get(item.trust_level, 0) < TRUST_RANK.get(query.trust_min, 0):
                continue
            if query.query_text.lower() not in item.content.lower() and query.query_text.strip():
                # soft lexical gate; ranking will refine
                tokens = set(re.findall(r"[a-z0-9_]+", query.query_text.lower()))
                if tokens and not tokens.intersection(re.findall(r"[a-z0-9_]+", item.content.lower())):
                    continue
            collected.append(
                KnowledgeResult(
                    knowledge_id=item.knowledge_id,
                    content=item.content,
                    score=0.6,
                    source_id=item.source_id,
                    source_type=src.source_type,
                    trust_level=item.trust_level,
                    freshness=freshness_label(stale=stale, policy=src.refresh_policy),
                    stale=stale,
                    confidence=item.confidence,
                    provenance=item.provenance,
                    citation_ref=item.citation_ref,
                    metadata_safe={"persisted": True},
                )
            )
            if stale:
                self._emit("knowledge.stale", status="stale", metadata={"source_type": src.source_type})
                self._metric("knowledge_stale_result_total", src.source_type, "included", item.trust_level)

        for src in sources:
            # Non-local sources must match query scope exactly.
            if src.source_type not in {"memory", "document", "local_file"}:
                if src.scope.key() != query.scope.key():
                    continue
            adapter = self.registry.get_adapter(src.source_id)
            if adapter is None:
                continue
            if src.source_type == "search_provider" and not query.allow_ephemeral_external:
                continue
            try:
                rows = adapter.fetch(
                    query=query.query_text,
                    scope=query.scope,
                    limit=query.limit,
                    now=stamp,
                )
            except Exception:
                continue
            for row in rows:
                if query.trust_min and TRUST_RANK.get(row.trust_level, 0) < TRUST_RANK.get(
                    query.trust_min, 0
                ):
                    continue
                collected.append(row)

        ranked = merge_and_rank(collected, query=query.query_text, limit=min(query.limit, self.max_results))
        conflicts = detect_conflicts(ranked)
        if conflicts:
            self._emit(
                "knowledge.conflict_detected",
                status="conflict",
                metadata={"count": len(conflicts)},
            )

        elapsed = max(0, int((utc_now() - started).total_seconds() * 1000))
        self._emit(
            "knowledge.retrieved",
            status="ok",
            metadata={"count": len(ranked), "source_type": "hybrid"},
        )
        self._metric("knowledge_retrieval_total", "hybrid", "ok", "n_a")
        obs = self.observability
        if obs is not None and getattr(obs, "metrics", None):
            try:
                obs.metrics.observe_latency(
                    "knowledge_retrieval_latency_ms",
                    float(elapsed),
                    labels={
                        "component": "knowledge",
                        "source_type": "hybrid",
                        "trust_level": "n_a",
                        "status": "ok",
                    },
                )
            except Exception:
                pass

        self._cache[cache_key] = (stamp.timestamp(), ranked)
        return ranked

    def build_rag_context(self, results: tuple[KnowledgeResult, ...] | list[KnowledgeResult]):
        return self.rag_builder.build(
            results,
            max_items=self.max_results,
            max_context_bytes=self.max_context_bytes,
        )

    def retrieve_knowledge_context(
        self,
        query: KnowledgeQuery,
        *,
        requesting_scope: MemoryScope | None = None,
    ):
        results = self.retrieve(query, requesting_scope=requesting_scope)
        return self.build_rag_context(results)

    def refresh_source(
        self,
        source_id: str,
        *,
        requesting_scope: MemoryScope,
        query_text: str = "",
    ) -> dict:
        self._require_access(requesting_scope, requesting_scope, OP_REFRESH)
        source = self.registry.get(source_id)
        if source.scope.key() != requesting_scope.key():
            raise KnowledgeAccessDenied("cross_scope_denied")
        if not source.enabled:
            raise KnowledgeDenied("source_disabled")
        self._emit("knowledge.refresh_started", status="started", metadata={"source_type": source.source_type})
        self._metric("knowledge_refresh_total", source.source_type, "started", source.trust_level)
        adapter = self.registry.get_adapter(source_id)
        try:
            rows = ()
            if adapter is not None and query_text:
                rows = adapter.fetch(query=query_text, scope=source.scope, limit=self.max_results)
            self._cache.clear()
            self._emit(
                "knowledge.refresh_completed",
                status="ok",
                metadata={"source_type": source.source_type, "count": len(rows)},
            )
            self._metric("knowledge_refresh_total", source.source_type, "ok", source.trust_level)
            return {"source_id": source_id, "refreshed": True, "count": len(rows)}
        except Exception:
            self._emit("knowledge.refresh_failed", status="failed", metadata={"source_type": source.source_type})
            self._metric("knowledge_refresh_failure_total", source.source_type, "failed", source.trust_level)
            raise KnowledgeDenied("refresh_failed")

    def _cache_key(self, query: KnowledgeQuery) -> str:
        raw = "|".join(
            [
                query.scope.scope_type,
                query.scope.scope_id,
                query.query_text,
                ",".join(query.source_ids),
                ",".join(query.source_types),
                str(query.trust_min),
                str(query.freshness_required),
                str(query.include_stale),
                str(query.allow_ephemeral_external),
                str(query.limit),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _require_access(self, requesting, target, operation) -> None:
        try:
            self.access.require(requesting=requesting, target=target, operation=operation)
        except KnowledgeAccessDenied:
            self._emit(
                "knowledge.denied",
                status="denied",
                metadata={
                    "reason_code": "knowledge_scope_access_denied",
                    "operation": operation,
                    "scope_type": requesting.scope_type,
                },
            )
            self._metric("knowledge_denied_total", "n_a", "denied", "n_a")
            raise

    def _looks_like_secret(self, content: str) -> bool:
        if any(m in content for m in _SECRET_MARKERS):
            return True
        return bool(re.search(r"(?i)\b(api[_-]?key|password|secret)\b\s*[:=]", content))

    def _looks_like_poison(self, content: str) -> bool:
        lowered = content.lower()
        return any(p in lowered for p in _POISON)

    def _emit(self, event_type: str, *, status: str = "", metadata: dict | None = None) -> None:
        obs = self.observability
        if obs is None:
            return
        try:
            safe = sanitize_metadata(
                {
                    k: v
                    for k, v in dict(metadata or {}).items()
                    if k not in {"content", "query", "scope_id", "url", "raw_url"}
                }
            )
            obs.emit(event_type, component="knowledge", status=status, metadata=safe)
        except Exception:
            pass

    def _metric(self, name: str, source_type: str, status: str, trust_level: str) -> None:
        obs = self.observability
        if obs is None or not getattr(obs, "metrics", None):
            return
        try:
            obs.metrics.inc(
                name,
                labels={
                    "component": "knowledge",
                    "source_type": source_type or "unknown",
                    "trust_level": trust_level or "unknown",
                    "status": status,
                },
            )
        except Exception:
            pass
