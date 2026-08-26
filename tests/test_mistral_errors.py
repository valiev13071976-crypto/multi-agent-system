"""Mistral error classification tests."""

from __future__ import annotations

import unittest

from agents.mistral_errors import (
    MISTRAL_AUTH,
    MISTRAL_CONTEXT_LENGTH,
    MISTRAL_RATE_LIMIT,
    classify_http_status,
    error_from_http_status,
)


class MistralErrorsTests(unittest.TestCase):
    def test_auth_non_retryable(self):
        cat, retryable = classify_http_status(401)
        self.assertEqual(cat, MISTRAL_AUTH)
        self.assertFalse(retryable)
        err = error_from_http_status(403)
        self.assertEqual(err.category, MISTRAL_AUTH)
        self.assertFalse(err.retryable)

    def test_429_retryable_bounded(self):
        cat, retryable = classify_http_status(429)
        self.assertEqual(cat, MISTRAL_RATE_LIMIT)
        self.assertTrue(retryable)

    def test_context_length(self):
        err = error_from_http_status(413)
        self.assertEqual(err.category, MISTRAL_CONTEXT_LENGTH)


if __name__ == "__main__":
    unittest.main()
