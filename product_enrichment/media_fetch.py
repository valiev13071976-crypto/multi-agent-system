"""Governed, SSRF-safe RAW BINARY image download (requirement 6/10).

The existing ``scrape.fetch`` tool (``tools.platform.web_fetch_adapter.
WebFetchAdapter``) is Panda's governed URL-fetch capability, but it always
UTF-8-decodes the response body (``body_text``) -- correct for HTML/JSON,
but it corrupts binary image content. This module is the one genuinely
missing adapter: it reuses the EXACT SAME SSRF validator
(``tools.url_safety.validate_http_url``, revalidated on every redirect hop,
mirroring ``WebFetchAdapter``'s own redirect handling) plus equivalent
timeout/size/redirect bounds, but returns raw bytes.

No new trust boundary is introduced -- this is not a second, laxer fetch
path; it is the same boundary, adapted for the one payload shape
``scrape.fetch`` cannot carry safely.
"""

from __future__ import annotations

from typing import Protocol

from tools.url_safety import UnsafeUrlError, validate_http_url

DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # matches product_media's own sync image bound
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_REDIRECTS = 5


class MediaFetchError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ImageFetchPort(Protocol):
    async def fetch_bytes(self, url: str) -> bytes: ...


class GovernedImageFetcher:
    """Production binary fetcher. Real tests inject a deterministic
    ``transport`` (``httpx.MockTransport`` -- same pattern already used by
    ``tools.platform.web_fetch_adapter.WebFetchAdapter``) instead of hitting
    the network; unit-level enrichment tests use the even simpler
    ``FakeImageFetcher`` below."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        transport=None,
    ):
        self._max_bytes = max_bytes
        self._timeout_seconds = timeout_seconds
        self._max_redirects = max_redirects
        self._transport = transport

    async def fetch_bytes(self, url: str) -> bytes:
        import httpx

        try:
            current = validate_http_url(url)
        except UnsafeUrlError as exc:
            raise MediaFetchError(f"unsafe_url:{exc.reason}") from exc

        for _hop in range(self._max_redirects + 1):
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, follow_redirects=False, transport=self._transport
            ) as client:
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location") or ""
                        if not location:
                            raise MediaFetchError("redirect_without_location")
                        try:
                            current = validate_http_url(location)
                        except UnsafeUrlError as exc:
                            raise MediaFetchError(f"unsafe_redirect:{exc.reason}") from exc
                        continue
                    if resp.status_code != 200:
                        raise MediaFetchError(f"http_status_{resp.status_code}")
                    chunks = bytearray()
                    async for chunk in resp.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > self._max_bytes:
                            raise MediaFetchError("response_too_large")
                    return bytes(chunks)
        raise MediaFetchError("too_many_redirects")


class FakeImageFetcher:
    """Deterministic, no-network test double -- mirrors
    ``tools.search.fake_provider.FakeSearchProvider``'s style. URLs not
    present in ``bytes_by_url`` raise ``MediaFetchError`` (download
    failure), never silently returning empty/placeholder bytes."""

    def __init__(self, bytes_by_url: dict[str, bytes] | None = None, *, error_urls: tuple[str, ...] = ()):
        self.bytes_by_url = dict(bytes_by_url or {})
        self.error_urls = set(error_urls)
        self.requested_urls: list[str] = []

    async def fetch_bytes(self, url: str) -> bytes:
        self.requested_urls.append(url)
        validate_http_url(url)  # same SSRF check the real fetcher applies
        if url in self.error_urls:
            raise MediaFetchError("simulated_download_failure")
        data = self.bytes_by_url.get(url)
        if data is None:
            raise MediaFetchError("not_found")
        return data
