"""Deterministic hybrid ranking / merge / diversity."""

from __future__ import annotations

import re

from knowledge.models import TRUST_RANK, TRUST_UNVERIFIED, KnowledgeResult, content_hash_text

_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.I)


def retrieval_policy_snapshot() -> dict:
    return {
        "knowledge_retrieval_version": "1.0.0",
        "factors": [
            "token_overlap",
            "trust",
            "freshness",
            "confidence",
            "recency",
            "source_diversity",
        ],
        "trust_cannot_be_overridden_by_relevance_alone": True,
        "external_embeddings": False,
        "external_vector_db": False,
    }


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def score_result(result: KnowledgeResult, query: str) -> float:
    q = _tokens(query)
    t = _tokens(result.content)
    overlap = (len(q & t) / max(1, len(q))) if q else 0.0
    if q and overlap <= 0 and query.lower() in result.content.lower():
        overlap = 0.25
    trust = TRUST_RANK.get(result.trust_level, 1) / 6.0
    conf = float(result.confidence) if result.confidence is not None else 0.5
    fresh = 0.2 if result.stale else 0.8
    # High relevance alone cannot dominate very low trust
    if result.trust_level == TRUST_UNVERIFIED:
        overlap = min(overlap, 0.55)
    return round(overlap * 2.0 + trust * 1.2 + conf * 0.5 + fresh * 0.4 + float(result.score) * 0.3, 6)


def merge_and_rank(
    results: list[KnowledgeResult],
    *,
    query: str,
    limit: int,
) -> tuple[KnowledgeResult, ...]:
    # Dedup same source + same content hash; keep corroborating distinct sources
    seen_same_source: set[tuple[str, str]] = set()
    deduped: list[KnowledgeResult] = []
    for row in results:
        digest = content_hash_text(row.content)
        key = (row.source_id, digest)
        if key in seen_same_source:
            continue
        seen_same_source.add(key)
        deduped.append(row)

    scored = []
    source_counts: dict[str, int] = {}
    for row in deduped:
        base = score_result(row, query)
        count = source_counts.get(row.source_id, 0)
        diversity_penalty = 0.15 * count
        scored.append((base - diversity_penalty, row))
        source_counts[row.source_id] = count + 1

    scored.sort(key=lambda t: (-t[0], t[1].citation_ref, t[1].knowledge_id))
    out = []
    for score, row in scored[:limit]:
        out.append(
            KnowledgeResult(
                knowledge_id=row.knowledge_id,
                content=row.content,
                score=score,
                source_id=row.source_id,
                source_type=row.source_type,
                trust_level=row.trust_level,
                freshness=row.freshness,
                stale=row.stale,
                confidence=row.confidence,
                provenance=row.provenance,
                citation_ref=row.citation_ref,
                metadata_safe=dict(row.metadata_safe),
            )
        )
    return tuple(out)


def detect_conflicts(results: tuple[KnowledgeResult, ...]) -> tuple[tuple[str, str], ...]:
    """Return pairs of citation_refs that conflict on numeric/simple facts (best-effort)."""
    pairs = []
    # Simple heuristic: same first token subject with different trailing numbers
    by_subj: dict[str, list[KnowledgeResult]] = {}
    for row in results:
        words = row.content.strip().split()
        if len(words) < 2:
            continue
        subj = words[0].lower()
        by_subj.setdefault(subj, []).append(row)
    for rows in by_subj.values():
        if len(rows) < 2:
            continue
        texts = {r.content.strip() for r in rows}
        if len(texts) > 1:
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    if rows[i].content.strip() != rows[j].content.strip():
                        pairs.append((rows[i].citation_ref, rows[j].citation_ref))
    return tuple(pairs)
