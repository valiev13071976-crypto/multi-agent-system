"""Unit tests for knowledge ranking and merge."""

from __future__ import annotations

import unittest

from knowledge.models import (
    TRUST_OPERATOR,
    TRUST_UNVERIFIED,
    KnowledgeProvenance,
    KnowledgeResult,
)
from knowledge.ranking import merge_and_rank, score_result
from memory.models import utc_now


def _result(
    *,
    knowledge_id: str,
    content: str,
    trust_level: str,
    stale: bool = False,
    score: float = 0.5,
    source_id: str = "src",
    citation_ref: str | None = None,
) -> KnowledgeResult:
    stamp = utc_now()
    return KnowledgeResult(
        knowledge_id=knowledge_id,
        content=content,
        score=score,
        source_id=source_id,
        source_type="manual_reference",
        trust_level=trust_level,
        freshness="stale" if stale else "static",
        stale=stale,
        provenance=KnowledgeProvenance(
            source_id=source_id,
            source_type="manual_reference",
            source_ref=f"ref:{knowledge_id}",
            ingested_at=stamp,
            trust_level=trust_level,
        ),
        citation_ref=citation_ref or f"knowledge:{knowledge_id}",
    )


class KnowledgeRankingTests(unittest.TestCase):
    def test_trust_outranks_unverified_lexical_edge(self):
        query = "widget retention policy details"
        unverified = _result(
            knowledge_id="u1",
            content="widget retention policy details extra overlap tokens",
            trust_level=TRUST_UNVERIFIED,
            score=0.95,
            source_id="ext",
            citation_ref="external:ext:u1",
        )
        trusted = _result(
            knowledge_id="t1",
            content="widget retention overview",
            trust_level=TRUST_OPERATOR,
            score=0.4,
            source_id="manual",
        )
        unverified_score = score_result(unverified, query)
        trusted_score = score_result(trusted, query)
        self.assertGreater(trusted_score, unverified_score)

        ranked = merge_and_rank([unverified, trusted], query=query, limit=2)
        self.assertEqual(ranked[0].knowledge_id, "t1")

    def test_freshness_affects_ranking(self):
        query = "widget index"
        fresh = _result(
            knowledge_id="f1",
            content="widget index is stable",
            trust_level=TRUST_UNVERIFIED,
            stale=False,
            source_id="a",
            citation_ref="external:a:f1",
        )
        stale = _result(
            knowledge_id="s1",
            content="widget index is stable",
            trust_level=TRUST_UNVERIFIED,
            stale=True,
            source_id="b",
            citation_ref="external:b:s1",
        )
        self.assertGreater(score_result(fresh, query), score_result(stale, query))

    def test_deterministic_order(self):
        query = "alpha"
        rows = [
            _result(
                knowledge_id="b",
                content="alpha beta gamma",
                trust_level=TRUST_OPERATOR,
                score=0.5,
                source_id="s1",
                citation_ref="knowledge:b",
            ),
            _result(
                knowledge_id="a",
                content="alpha beta delta",
                trust_level=TRUST_OPERATOR,
                score=0.5,
                source_id="s2",
                citation_ref="knowledge:a",
            ),
        ]
        first = merge_and_rank(rows, query=query, limit=2)
        second = merge_and_rank(list(reversed(rows)), query=query, limit=2)
        self.assertEqual(
            [r.knowledge_id for r in first],
            [r.knowledge_id for r in second],
        )


if __name__ == "__main__":
    unittest.main()
