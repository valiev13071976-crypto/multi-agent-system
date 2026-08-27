"""Deterministic memory retrieval without external embeddings."""

from __future__ import annotations

import re
from datetime import datetime

from memory.models import (
    MEMORY_EPISODIC,
    MEMORY_PROCEDURAL,
    MEMORY_SEMANTIC,
    MEMORY_WORKING_REFERENCE,
    MemoryQuery,
    MemorySearchResult,
    SOURCE_DOCUMENT,
    SOURCE_EXTERNAL,
    SOURCE_OPERATOR,
    SOURCE_SYSTEM,
    SOURCE_TOOL_RESULT,
    SOURCE_WORKFLOW_RESULT,
    citation_ref_for,
    utc_now,
)
from security.encryption import SENSITIVITY_SECRET, EncryptionService


_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.I)

TYPE_WEIGHT = {
    MEMORY_SEMANTIC: 1.2,
    MEMORY_PROCEDURAL: 1.15,
    MEMORY_EPISODIC: 1.0,
    MEMORY_WORKING_REFERENCE: 0.9,
}

SOURCE_WEIGHT = {
    SOURCE_OPERATOR: 1.3,
    SOURCE_SYSTEM: 1.25,
    SOURCE_WORKFLOW_RESULT: 1.2,
    SOURCE_TOOL_RESULT: 1.05,
    SOURCE_DOCUMENT: 1.0,
    SOURCE_EXTERNAL: 0.7,
}


class MemoryRetriever:
    retrieval_version = "1.0.0"

    def __init__(self, *, encryption: EncryptionService | None = None):
        self.encryption = encryption
        self.fts_fallback_active = False

    def search(self, store, query: MemoryQuery, *, now: datetime | None = None) -> tuple[MemorySearchResult, ...]:
        stamp = now or utc_now()
        from memory.models import STATUS_ACTIVE, STATUS_EXPIRED, STATUS_SUPERSEDED

        statuses = [STATUS_ACTIVE]
        if query.include_superseded:
            statuses.append(STATUS_SUPERSEDED)
        if query.include_expired:
            statuses.append(STATUS_EXPIRED)
        candidates = list(store.list_by_scope(query.scope, statuses=tuple(statuses)))
        # Expire normalization (local only)
        kept = []
        for row in candidates:
            if row.status == STATUS_ACTIVE and row.expires_at is not None and row.expires_at <= stamp:
                try:
                    store.expire(row.memory_id, now=stamp, scope=query.scope)
                except Exception:
                    pass
                if not query.include_expired:
                    continue
            elif row.status == STATUS_EXPIRED and not query.include_expired:
                continue
            elif row.status == STATUS_SUPERSEDED and not query.include_superseded:
                continue
            kept.append(row)
        candidates = kept

        if query.memory_types:
            allowed = set(query.memory_types)
            candidates = [c for c in candidates if c.memory_type in allowed]
        if query.source_types:
            allowed_s = set(query.source_types)
            candidates = [c for c in candidates if c.source_type in allowed_s]
        if query.tags:
            want = set(query.tags)
            candidates = [c for c in candidates if want.intersection(c.tags)]
        if query.min_confidence is not None:
            candidates = [
                c
                for c in candidates
                if c.confidence is not None and c.confidence >= query.min_confidence
            ]

        fts_ids: tuple[str, ...] = ()
        if hasattr(store, "fts_search_ids") and getattr(store, "fts_available", False):
            fts_ids = store.fts_search_ids(
                query.query_text, scope=query.scope, limit=query.limit * 3
            )
        else:
            self.fts_fallback_active = True

        q_tokens = set(_TOKEN_RE.findall(query.query_text.lower()))
        scored: list[tuple[float, object]] = []
        for row in candidates:
            text = self._visible_text(row)
            if row.sensitivity == SENSITIVITY_SECRET and not text:
                continue
            tokens = set(_TOKEN_RE.findall(text.lower()))
            overlap = len(q_tokens & tokens) / max(1, len(q_tokens)) if q_tokens else 0.0
            if fts_ids and row.memory_id in fts_ids:
                overlap = max(overlap, 0.6)
            if q_tokens and overlap <= 0 and query.query_text.strip():
                # allow empty-query listing? if query empty, keep all
                if query.query_text.strip():
                    # substring soft match
                    if query.query_text.lower() not in text.lower():
                        continue
                    overlap = 0.2
            conf = float(row.confidence) if row.confidence is not None else 0.5
            age_hours = max(0.0, (stamp - row.created_at).total_seconds() / 3600.0)
            recency = 1.0 / (1.0 + age_hours / 24.0)
            score = (
                overlap * 2.0
                + conf * 0.8
                + recency * 0.5
                + TYPE_WEIGHT.get(row.memory_type, 1.0) * 0.2
                + SOURCE_WEIGHT.get(row.source_type, 0.8) * 0.3
            )
            if query.tags:
                score += 0.1 * len(set(query.tags).intersection(row.tags))
            scored.append((score, row))

        scored.sort(key=lambda t: (-t[0], t[1].memory_id))
        results = []
        for score, row in scored[: query.limit]:
            results.append(
                MemorySearchResult(
                    memory_id=row.memory_id,
                    score=round(float(score), 6),
                    memory_type=row.memory_type,
                    content_or_summary=self._visible_text(row),
                    provenance=row.provenance,
                    confidence=row.confidence,
                    created_at=row.created_at,
                    source_ref=row.source_ref,
                    citation_ref=citation_ref_for(row.memory_id),
                    sensitivity=row.sensitivity,
                    tags=row.tags,
                )
            )
        return tuple(results)

    def _visible_text(self, row) -> str:
        if row.content_safe:
            return row.content_safe
        if row.summary_safe:
            return row.summary_safe
        if row.encrypted_content and self.encryption is not None:
            try:
                return self.encryption.decrypt(row.encrypted_content)
            except Exception:
                return ""
        return ""


def retrieval_policy_snapshot() -> dict:
    return {
        "memory_retrieval_version": "1.0.0",
        "ranking": [
            "token_overlap",
            "fts_optional",
            "confidence",
            "recency",
            "type_weight",
            "source_weight",
            "tag_match",
        ],
        "type_weight": dict(TYPE_WEIGHT),
        "source_weight": dict(SOURCE_WEIGHT),
        "external_embeddings": False,
    }
