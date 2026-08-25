"""Unit tests for ZIP expansion safety limits."""

from __future__ import annotations

import io
import unittest
import zipfile

from documents.errors import DocumentError
from documents.zip_safety import inspect_zip_safety


class DocumentZipSafetyTests(unittest.TestCase):
    def test_too_many_entries_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for i in range(50):
                zf.writestr(f"f{i}.txt", "x")
        with self.assertRaises(DocumentError) as ctx:
            inspect_zip_safety(buf.getvalue(), max_entries=10)
        self.assertEqual(ctx.exception.reason, "archive_expansion_limit_exceeded")

    def test_zip_slip_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.txt", "x")
        with self.assertRaises(DocumentError) as ctx:
            inspect_zip_safety(buf.getvalue())
        self.assertEqual(ctx.exception.reason, "archive_expansion_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
