"""Bounded structured knowledge context for agents — data, not prompt injection."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from memory.models import MemorySearchResult
from security.encryption import SENSITIVITY_SECRET


@dataclass(frozen=True)
class KnowledgeContextItem:
    memory_id: str
    citation_ref: str
    memory_type: str
    content: str
    provenance_source_type: str
    provenance_source_id: str
    confidence: float | None
    trust_hint: str
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "metadata_safe", MappingProxyType(sanitize_metadata(self.metadata_safe or {}))
        )


@dataclass(frozen=True)
class KnowledgeContext:
    items: tuple[KnowledgeContextItem, ...]
    untrusted_data: bool = True
    policy_override_forbidden: bool = True

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items))


class KnowledgeContextBuilder:
    """Build bounded context; marks content as untrusted data by default."""

    def build(
        self,
        results: tuple[MemorySearchResult, ...] | list[MemorySearchResult],
        *,
        max_items: int = 10,
        max_chars_per_item: int = 2000,
    ) -> KnowledgeContext:
        items = []
        for row in list(results)[: max(1, int(max_items))]:
            if row.sensitivity == SENSITIVITY_SECRET:
                continue
            text = str(row.content_or_summary or "")[: int(max_chars_per_item)]
            trust = "elevated" if row.provenance.source_type in {
                "operator",
                "system_generated",
                "workflow_result",
            } else "untrusted"
            items.append(
                KnowledgeContextItem(
                    memory_id=row.memory_id,
                    citation_ref=row.citation_ref,
                    memory_type=row.memory_type,
                    content=text,
                    provenance_source_type=row.provenance.source_type,
                    provenance_source_id=row.provenance.source_id,
                    confidence=row.confidence,
                    trust_hint=trust,
                    metadata_safe={
                        "score": row.score,
                        "source_ref": row.source_ref,
                    },
                )
            )
        return KnowledgeContext(items=tuple(items))
