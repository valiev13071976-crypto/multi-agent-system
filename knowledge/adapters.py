"""Read-only knowledge source adapters."""

from __future__ import annotations

import asyncio

from knowledge.models import (
    SOURCE_DOCUMENT,
    SOURCE_MEMORY,
    SOURCE_SEARCH_PROVIDER,
    TRUST_DOCUMENT,
    TRUST_OPERATOR,
    TRUST_READ_ONLY_EXTERNAL,
    TRUST_SYSTEM,
    TRUST_UNVERIFIED,
    KnowledgeProvenance,
    KnowledgeResult,
    citation_ref_for,
    content_hash_text,
    utc_now,
)
from memory.models import MemoryQuery
from tools.url_safety import UnsafeUrlError, validate_http_url


class MemoryKnowledgeAdapter:
    """Local MemoryService-backed source — no network."""

    def __init__(self, memory_service, *, source_id: str):
        self.memory_service = memory_service
        self.source_id = source_id

    def fetch(self, *, query: str, scope, limit: int = 10, now=None) -> tuple[KnowledgeResult, ...]:
        _ = now
        if self.memory_service is None:
            return ()
        hits = self.memory_service.retrieve(
            MemoryQuery(query_text=query, scope=scope, limit=limit),
            requesting_scope=scope,
        )
        out = []
        for h in hits:
            src = h.provenance.source_type
            if src == "operator":
                trust = TRUST_OPERATOR
            elif src == "system_generated":
                trust = TRUST_SYSTEM
            elif src == "document":
                trust = TRUST_DOCUMENT
            else:
                trust = TRUST_DOCUMENT
            stamp = utc_now()
            prov = KnowledgeProvenance(
                source_id=self.source_id,
                source_type=SOURCE_MEMORY,
                source_ref=h.source_ref or h.memory_id,
                ingested_at=h.created_at,
                source_hash=content_hash_text(h.content_or_summary),
                trust_level=trust,
                validation_state="memory",
                retrieved_at=stamp,
            )
            out.append(
                KnowledgeResult(
                    knowledge_id=h.memory_id,
                    content=h.content_or_summary,
                    score=float(h.score),
                    source_id=self.source_id,
                    source_type=SOURCE_MEMORY,
                    trust_level=trust,
                    freshness="static",
                    stale=False,
                    confidence=h.confidence,
                    provenance=prov,
                    citation_ref=citation_ref_for(memory_id=h.memory_id),
                    metadata_safe={"memory_type": h.memory_type},
                )
            )
        return tuple(out)

    def inspect(self) -> dict:
        return {"adapter": "memory", "source_id": self.source_id}

    def health(self) -> dict:
        return {"status": "healthy" if self.memory_service is not None else "degraded"}


class DocumentKnowledgeAdapter:
    """P14 DocumentService chunk retrieval — no network, no raw paths."""

    def __init__(self, document_service, *, source_id: str):
        self.document_service = document_service
        self.source_id = source_id

    def fetch(self, *, query: str, scope, limit: int = 10, now=None) -> tuple[KnowledgeResult, ...]:
        _ = now
        if self.document_service is None:
            return ()
        from documents.models import DocumentSearchRequest

        hits = self.document_service.search(
            DocumentSearchRequest(scope=scope, query=query, limit=limit),
            requesting_scope=scope,
        )
        stamp = utc_now()
        out = []
        for h in hits:
            prov = KnowledgeProvenance(
                source_id=self.source_id,
                source_type=SOURCE_DOCUMENT,
                source_ref=h.source_location,
                ingested_at=stamp,
                source_hash=content_hash_text(h.snippet_safe),
                trust_level=TRUST_DOCUMENT,
                validation_state="document",
                document_id=h.document_id,
                chunk_id=h.chunk_id,
                retrieved_at=stamp,
            )
            out.append(
                KnowledgeResult(
                    knowledge_id=h.chunk_id,
                    content=h.snippet_safe,
                    score=float(h.score),
                    source_id=self.source_id,
                    source_type=SOURCE_DOCUMENT,
                    trust_level=TRUST_DOCUMENT,
                    freshness="static",
                    stale=False,
                    provenance=prov,
                    citation_ref=h.citation_ref,
                    metadata_safe={"document_id": h.document_id},
                )
            )
        return tuple(out)

    def inspect(self) -> dict:
        return {"adapter": "document", "source_id": self.source_id}

    def health(self) -> dict:
        return {"status": "healthy" if self.document_service is not None else "degraded"}


class SearchProviderKnowledgeAdapter:
    """External search ONLY via ToolGateway — never direct httpx."""

    def __init__(self, tool_gateway, *, source_id: str, allowed_base_urls: tuple[str, ...] = ()):
        self.tool_gateway = tool_gateway
        self.source_id = source_id
        self.allowed_base_urls = tuple(allowed_base_urls)
        for url in self.allowed_base_urls:
            validate_http_url(url)

    def fetch(self, *, query: str, scope, limit: int = 10, now=None) -> tuple[KnowledgeResult, ...]:
        _ = scope
        _ = now
        if self.tool_gateway is None:
            return ()
        rows = self._search(query, limit=limit)
        out = []
        for item in rows:
            try:
                validate_http_url(item.url)
            except UnsafeUrlError:
                continue
            text = f"{item.title}\n{item.snippet}".strip()
            trust = TRUST_UNVERIFIED
            if getattr(item, "trust_level", "") in {"high", "medium"}:
                trust = TRUST_READ_ONLY_EXTERNAL
            prov = KnowledgeProvenance(
                source_id=self.source_id,
                source_type=SOURCE_SEARCH_PROVIDER,
                source_ref=item.url,
                ingested_at=item.retrieved_at,
                source_hash=content_hash_text(text),
                trust_level=trust,
                validation_state="unvalidated",
                tool_id="search",
                external_reference=item.url,
                retrieved_at=item.retrieved_at,
            )
            kid = content_hash_text(item.url + text)[:16]
            out.append(
                KnowledgeResult(
                    knowledge_id=kid,
                    content=text,
                    score=0.5,
                    source_id=self.source_id,
                    source_type=SOURCE_SEARCH_PROVIDER,
                    trust_level=trust,
                    freshness="on_demand",
                    stale=False,
                    provenance=prov,
                    citation_ref=citation_ref_for(
                        source_id=self.source_id, safe_ref=item.source_domain or kid
                    ),
                    metadata_safe={"domain": item.source_domain},
                )
            )
        return tuple(out)

    def _search(self, query: str, *, limit: int):
        gw = self.tool_gateway
        if not hasattr(gw, "search"):
            return []

        def _invoke():
            result = gw.search(query, max_results=limit)
            if asyncio.iscoroutine(result):
                return asyncio.run(result)
            return result

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _invoke()
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_invoke).result(timeout=30)

    def inspect(self) -> dict:
        return {"adapter": "search_provider", "source_id": self.source_id, "via": "tool_gateway"}

    def health(self) -> dict:
        return {"status": "healthy" if self.tool_gateway is not None else "degraded"}


class ManualReferenceAdapter:
    """Static operator/manual references — no network."""

    def __init__(self, *, source_id: str, items: tuple[tuple[str, str], ...] = ()):
        self.source_id = source_id
        self.items = items

    def fetch(self, *, query: str, scope, limit: int = 10, now=None) -> tuple[KnowledgeResult, ...]:
        _ = scope
        _ = now
        q = query.lower()
        out = []
        for ref, content in self.items[:limit]:
            if q and q not in content.lower() and q not in ref.lower():
                continue
            stamp = utc_now()
            prov = KnowledgeProvenance(
                source_id=self.source_id,
                source_type="manual_reference",
                source_ref=ref,
                ingested_at=stamp,
                source_hash=content_hash_text(content),
                trust_level=TRUST_OPERATOR,
                validation_state="operator",
                retrieved_at=stamp,
            )
            out.append(
                KnowledgeResult(
                    knowledge_id=content_hash_text(ref + content)[:16],
                    content=content,
                    score=1.0 if q in content.lower() else 0.4,
                    source_id=self.source_id,
                    source_type="manual_reference",
                    trust_level=TRUST_OPERATOR,
                    freshness="static",
                    stale=False,
                    provenance=prov,
                    citation_ref=citation_ref_for(source_id=self.source_id, safe_ref=ref),
                )
            )
        return tuple(out)

    def inspect(self) -> dict:
        return {"adapter": "manual_reference", "source_id": self.source_id}

    def health(self) -> dict:
        return {"status": "healthy"}
