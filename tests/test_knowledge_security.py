"""Unit tests for knowledge SSRF and URL query security."""

from __future__ import annotations

import unittest

from knowledge.models import (
    SOURCE_MANUAL_REFERENCE,
    SOURCE_READ_ONLY_EXTERNAL,
    TRUST_OPERATOR,
    TRUST_READ_ONLY_EXTERNAL,
    FreshnessPolicy,
    KnowledgeQuery,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.service import KnowledgeDenied, KnowledgeService
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class KnowledgeSecurityTests(unittest.TestCase):
    def setUp(self):
        registry = KnowledgeSourceRegistry()
        self.svc = KnowledgeService(registry)
        self.scope = _scope("sec")
        stamp = utc_now()
        self.svc.register_source(
            KnowledgeSource(
                source_id="manual.default",
                scope=self.scope,
                source_type=SOURCE_MANUAL_REFERENCE,
                name="Manual",
                trust_level=TRUST_OPERATOR,
                refresh_policy=FreshnessPolicy(policy="static"),
                created_at=stamp,
                updated_at=stamp,
            )
        )

    def test_ssrf_denied_on_register(self):
        stamp = utc_now()
        bad_urls = (
            ("bad-loopback", "http://127.0.0.1/secret"),
            ("bad-localhost", "http://localhost/x"),
            ("bad-metadata", "http://169.254.169.254/latest/meta-data"),
            ("bad-private", "http://10.0.0.5/internal"),
        )
        for sid, url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(KnowledgeDenied) as ctx:
                    self.svc.register_source(
                        KnowledgeSource(
                            source_id=sid,
                            scope=self.scope,
                            source_type=SOURCE_READ_ONLY_EXTERNAL,
                            name="bad",
                            trust_level=TRUST_READ_ONLY_EXTERNAL,
                            refresh_policy=FreshnessPolicy(policy="on_demand"),
                            created_at=stamp,
                            updated_at=stamp,
                            metadata_safe={"url": url},
                        )
                    )
                self.assertIn("ssrf_denied", ctx.exception.reason)

    def test_arbitrary_url_query_denied(self):
        with self.assertRaises(ValueError) as ctx:
            KnowledgeQuery(query_text="https://example.com/page", scope=self.scope)
        self.assertIn("arbitrary_url_query_denied", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
