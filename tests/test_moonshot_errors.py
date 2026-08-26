"""Moonshot error classification tests."""

from __future__ import annotations

import unittest

from agents.moonshot_errors import (
    MOONSHOT_AUTH,
    MOONSHOT_CONTEXT_LENGTH,
    MOONSHOT_RATE_LIMIT,
    classify_http_status,
    error_from_http_status,
)


class MoonshotErrorsTests(unittest.TestCase):
    def test_auth_non_retryable(self):
        cat, retryable = classify_http_status(401)
        self.assertEqual(cat, MOONSHOT_AUTH)
        self.assertFalse(retryable)
        err = error_from_http_status(403)
        self.assertEqual(err.category, MOONSHOT_AUTH)
        self.assertFalse(err.retryable)

    def test_429_retryable_bounded(self):
        cat, retryable = classify_http_status(429)
        self.assertEqual(cat, MOONSHOT_RATE_LIMIT)
        self.assertTrue(retryable)

    def test_context_length(self):
        err = error_from_http_status(413)
        self.assertEqual(err.category, MOONSHOT_CONTEXT_LENGTH)


if __name__ == "__main__":
    unittest.main()
