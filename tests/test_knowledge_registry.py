"""Unit tests for KnowledgeSourceRegistry."""

from __future__ import annotations

import unittest

from knowledge.models import (
    SOURCE_MANUAL_REFERENCE,
    TRUST_OPERATOR,
    FreshnessPolicy,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry, KnowledgeSourceRegistryError
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _source(source_id="manual.default", *, enabled=True, scope=None):
    stamp = utc_now()
    return KnowledgeSource(
        source_id=source_id,
        scope=scope or _scope(),
        source_type=SOURCE_MANUAL_REFERENCE,
        name="Manual",
        trust_level=TRUST_OPERATOR,
        enabled=enabled,
        refresh_policy=FreshnessPolicy(policy="static"),
        created_at=stamp,
        updated_at=stamp,
    )


class KnowledgeSourceRegistryTests(unittest.TestCase):
    def test_register_get_list(self):
        reg = KnowledgeSourceRegistry()
        src = _source("manual.a")
        reg.register(src)
        got = reg.get("manual.a")
        self.assertEqual(got.source_id, "manual.a")
        listed = reg.list_sources()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].source_id, "manual.a")

    def test_duplicate_source_denied(self):
        reg = KnowledgeSourceRegistry()
        reg.register(_source("manual.a"))
        with self.assertRaises(KnowledgeSourceRegistryError) as ctx:
            reg.register(_source("manual.a"))
        self.assertEqual(ctx.exception.reason, "duplicate_source")

    def test_disable_excludes_from_enabled_only(self):
        reg = KnowledgeSourceRegistry()
        reg.register(_source("manual.a"))
        reg.disable("manual.a")
        disabled = reg.get("manual.a")
        self.assertFalse(disabled.enabled)
        self.assertEqual(len(reg.list_sources(enabled_only=True)), 0)
        self.assertEqual(len(reg.list_sources(enabled_only=False)), 1)

    def test_enable_restores_source(self):
        reg = KnowledgeSourceRegistry()
        reg.register(_source("manual.a", enabled=False))
        reg.enable("manual.a")
        self.assertTrue(reg.get("manual.a").enabled)

    def test_freeze_blocks_register(self):
        reg = KnowledgeSourceRegistry()
        reg.register(_source("manual.a"))
        reg.freeze()
        self.assertTrue(reg.frozen)
        with self.assertRaises(KnowledgeSourceRegistryError) as ctx:
            reg.register(_source("manual.b"))
        self.assertEqual(ctx.exception.reason, "registry_frozen")

    def test_disable_allowed_after_freeze(self):
        reg = KnowledgeSourceRegistry()
        reg.register(_source("manual.a"))
        reg.freeze()
        reg.disable("manual.a")
        self.assertFalse(reg.get("manual.a").enabled)


if __name__ == "__main__":
    unittest.main()
