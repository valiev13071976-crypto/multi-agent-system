"""RAG context builder — structured untrusted data, not system prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from knowledge.models import TRUST_OPERATOR, TRUST_SYSTEM, TRUST_VALIDATED_INTERNAL, KnowledgeResult
from security.redaction import redact


@dataclass(frozen=True)
class RAGContextItem:
    content: str
    citation_ref: str
    source_type: str
    trust_level: str
    freshness: str
    provenance_summary: str
    untrusted_data: bool = True
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "metadata_safe", MappingProxyType(sanitize_metadata(self.metadata_safe or {}))
        )


@dataclass(frozen=True)
class RAGContext:
    items: tuple[RAGContextItem, ...]
    untrusted_data: bool = True
    policy_override_forbidden: bool = True
    total_bytes: int = 0

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items))


class RAGContextBuilder:
    """Build bounded citation-aware context; never elevates to system instructions."""

    def build(
        self,
        results: tuple[KnowledgeResult, ...] | list[KnowledgeResult],
        *,
        max_items: int = 10,
        max_chars_per_item: int = 2000,
        max_context_bytes: int = 64_000,
    ) -> RAGContext:
        items = []
        total = 0
        for row in list(results)[: max(1, int(max_items))]:
            text = redact(str(row.content or ""))[: int(max_chars_per_item)]
            if not text or text == "[REDACTED]":
                continue
            encoded = text.encode("utf-8")
            if total + len(encoded) > int(max_context_bytes):
                break
            total += len(encoded)
            elevated = row.trust_level in {
                TRUST_SYSTEM,
                TRUST_OPERATOR,
                TRUST_VALIDATED_INTERNAL,
            }
            items.append(
                RAGContextItem(
                    content=text,
                    citation_ref=row.citation_ref,
                    source_type=row.source_type,
                    trust_level=row.trust_level,
                    freshness=row.freshness,
                    provenance_summary=(
                        f"{row.provenance.source_type}:{row.provenance.source_ref}"
                    )[:200],
                    untrusted_data=not elevated,
                    metadata_safe={
                        "score": row.score,
                        "stale": row.stale,
                        "source_id": row.source_id,
                    },
                )
            )
        return RAGContext(items=tuple(items), total_bytes=total)
