"""Unit tests for document observability events and safe metadata."""

from __future__ import annotations

import unittest

from documents.models import SOURCE_OPERATOR, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from observability.runtime import build_observability_runtime
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="proj-obs"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


FORBIDDEN_META_KEYS = ("content", "filename", "scope_id", "path", "query")


class DocumentObservabilityTests(unittest.TestCase):
    def test_ingest_events_without_sensitive_metadata(self):
        obs = build_observability_runtime(env={})
        svc = DocumentService(InMemoryDocumentStore(), observability=obs)
        scope = _scope("obs-scope-xyz")
        filename = "secret-name-file.txt"
        body = b"unique observability body token"
        row = svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename=filename,
                content=body,
                source_type=SOURCE_OPERATOR,
                source_id="op-obs",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        events = obs.list_events()
        types = {e.event_type for e in events}
        self.assertIn("document.ingested", types)
        self.assertIn("document.parsed", types)
        self.assertIn("document.chunked", types)

        for event in events:
            if getattr(event, "component", "") not in ("documents", ""):
                # filter if component present
                pass
            meta = dict(event.metadata_safe or {})
            for key in FORBIDDEN_META_KEYS:
                self.assertNotIn(key, meta)
            blob = str(meta)
            self.assertNotIn(scope.scope_id, blob)
            self.assertNotIn(filename, blob)
            self.assertNotIn(body.decode("utf-8"), blob)
            self.assertNotIn(row.document_id, blob)


if __name__ == "__main__":
    unittest.main()
