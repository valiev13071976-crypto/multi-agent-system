"""Product enrichment pipeline: governed, SSRF-safe RAW BINARY image
download (requirements 6/10). ``GovernedImageFetcher`` reuses the exact
same ``tools.url_safety.validate_http_url`` boundary as the existing
``scrape.fetch`` tool -- these tests exercise it against a fully mocked
``httpx.MockTransport`` (same test pattern as
``tools.platform.web_fetch_adapter.WebFetchAdapter``), never a real
network call."""

from __future__ import annotations

import unittest

import httpx

from product_enrichment.media_fetch import (
    FakeImageFetcher,
    GovernedImageFetcher,
    MediaFetchError,
)
from tools.url_safety import UnsafeUrlError


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class GovernedImageFetcherTests(unittest.TestCase):
    def test_successful_binary_download_returns_exact_bytes(self):
        payload = b"\x89PNG\r\n\x1a\nfake-binary-body"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload)

        fetcher = GovernedImageFetcher(transport=httpx.MockTransport(handler))
        data = _run(fetcher.fetch_bytes("https://cdn.example.com/image.png"))
        self.assertEqual(data, payload)

    def test_unsafe_url_rejected_before_any_network_call(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must never be called for an unsafe URL")

        fetcher = GovernedImageFetcher(transport=httpx.MockTransport(handler))
        with self.assertRaises(MediaFetchError) as ctx:
            _run(fetcher.fetch_bytes("http://169.254.169.254/latest/meta-data/"))
        self.assertTrue(ctx.exception.code.startswith("unsafe_url:"))

    def test_redirect_is_followed_and_revalidated(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if str(request.url) == "https://cdn.example.com/original.jpg":
                return httpx.Response(302, headers={"location": "https://cdn.example.com/final.jpg"})
            return httpx.Response(200, content=b"final-image-bytes")

        fetcher = GovernedImageFetcher(transport=httpx.MockTransport(handler))
        data = _run(fetcher.fetch_bytes("https://cdn.example.com/original.jpg"))
        self.assertEqual(data, b"final-image-bytes")
        self.assertEqual(len(calls), 2)

    def test_redirect_into_private_ip_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})

        fetcher = GovernedImageFetcher(transport=httpx.MockTransport(handler))
        with self.assertRaises(MediaFetchError) as ctx:
            _run(fetcher.fetch_bytes("https://cdn.example.com/original.jpg"))
        self.assertTrue(ctx.exception.code.startswith("unsafe_redirect:"))

    def test_non_200_status_raises_media_fetch_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        fetcher = GovernedImageFetcher(transport=httpx.MockTransport(handler))
        with self.assertRaises(MediaFetchError) as ctx:
            _run(fetcher.fetch_bytes("https://cdn.example.com/missing.jpg"))
        self.assertEqual(ctx.exception.code, "http_status_404")

    def test_oversized_response_is_bounded_and_rejected(self):
        big_chunk = b"x" * (1024 * 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=big_chunk)

        fetcher = GovernedImageFetcher(max_bytes=1000, transport=httpx.MockTransport(handler))
        with self.assertRaises(MediaFetchError) as ctx:
            _run(fetcher.fetch_bytes("https://cdn.example.com/huge.jpg"))
        self.assertEqual(ctx.exception.code, "response_too_large")

    def test_too_many_redirects_is_bounded(self):
        counter = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            counter["n"] += 1
            return httpx.Response(302, headers={"location": f"https://cdn.example.com/hop{counter['n']}.jpg"})

        fetcher = GovernedImageFetcher(max_redirects=2, transport=httpx.MockTransport(handler))
        with self.assertRaises(MediaFetchError) as ctx:
            _run(fetcher.fetch_bytes("https://cdn.example.com/start.jpg"))
        self.assertEqual(ctx.exception.code, "too_many_redirects")


class FakeImageFetcherTests(unittest.TestCase):
    def test_returns_configured_bytes_for_known_url(self):
        fetcher = FakeImageFetcher({"https://lg.com/photo.jpg": b"bytes-here"})
        data = _run(fetcher.fetch_bytes("https://lg.com/photo.jpg"))
        self.assertEqual(data, b"bytes-here")
        self.assertEqual(fetcher.requested_urls, ["https://lg.com/photo.jpg"])

    def test_unknown_url_raises_not_found(self):
        fetcher = FakeImageFetcher({})
        with self.assertRaises(MediaFetchError) as ctx:
            _run(fetcher.fetch_bytes("https://lg.com/missing.jpg"))
        self.assertEqual(ctx.exception.code, "not_found")

    def test_explicit_error_url_raises_simulated_failure(self):
        fetcher = FakeImageFetcher({}, error_urls=("https://lg.com/broken.jpg",))
        with self.assertRaises(MediaFetchError) as ctx:
            _run(fetcher.fetch_bytes("https://lg.com/broken.jpg"))
        self.assertEqual(ctx.exception.code, "simulated_download_failure")

    def test_unsafe_url_raises_before_lookup_same_as_real_fetcher(self):
        fetcher = FakeImageFetcher({"http://127.0.0.1/x": b"should-never-be-returned"})
        with self.assertRaises(UnsafeUrlError):
            _run(fetcher.fetch_bytes("http://127.0.0.1/x"))


if __name__ == "__main__":
    unittest.main()
