"""Unit tests for document chunker bounds."""

from __future__ import annotations

import unittest

from documents.chunker import DocumentChunker
from documents.errors import DocumentError
from documents.models import ParsedDocument, TextBlock, content_hash_text
from memory.models import SCOPE_PROJECT, MemoryScope


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class DocumentChunkingTests(unittest.TestCase):
    def test_bounded_chunks_for_long_text(self):
        chunker = DocumentChunker(max_chars=40, overlap_chars=5, max_chunks=50)
        text = " ".join(f"word{i}" for i in range(80))
        parsed = ParsedDocument(
            document_id="d1",
            text_blocks=(
                TextBlock(
                    block_id="b1",
                    ordinal=0,
                    text=text,
                    content_hash=content_hash_text(text),
                    source_location="txt:0",
                ),
            ),
            tables=(),
            metadata_safe={},
            parser_id="txt_v1",
            parser_version="1.0.0",
        )
        chunks = chunker.chunk(parsed, scope=_scope())
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(len(ch.content_safe or ""), 40)

    def test_max_chunks_raises(self):
        chunker = DocumentChunker(max_chars=10, overlap_chars=0, max_chunks=2)
        text = "abcdefghij" * 5
        parsed = ParsedDocument(
            document_id="d2",
            text_blocks=(
                TextBlock(
                    block_id="b1",
                    ordinal=0,
                    text=text,
                    content_hash=content_hash_text(text),
                    source_location="txt:0",
                ),
            ),
            tables=(),
            metadata_safe={},
            parser_id="txt_v1",
            parser_version="1.0.0",
        )
        with self.assertRaises(DocumentError) as ctx:
            chunker.chunk(parsed, scope=_scope())
        self.assertEqual(ctx.exception.reason, "document_too_many_chunks")


if __name__ == "__main__":
    unittest.main()
