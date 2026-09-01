"""1C transport abstraction — HTTP/REST primary; others explicit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse


SUPPORTED_TRANSPORTS = frozenset({"http_rest", "odata"})
DEFERRED_TRANSPORTS = frozenset({"commerceml"})


@dataclass(frozen=True)
class TransportRequest:
    method: str
    path: str
    params: Mapping[str, Any] | None = None
    json_body: Mapping[str, Any] | None = None


class OneCTransport:
    """Canonical transport boundary — only configured base URL allowed (SSRF-safe)."""

    def __init__(self, *, base_url: str, transport: str = "http_rest"):
        self._base_url = base_url.rstrip("/") + "/"
        self._transport = transport
        if transport not in SUPPORTED_TRANSPORTS and transport not in DEFERRED_TRANSPORTS:
            raise ValueError("unsupported_onec_transport")
        self._allowed_host = (urlparse(base_url).hostname or "").lower()

    @property
    def transport(self) -> str:
        return self._transport

    def resolve_url(self, path: str) -> str:
        raw = str(path or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            host = (urlparse(raw).hostname or "").lower()
            if host != self._allowed_host:
                from integrations.onec.errors import OneCValidationError

                raise OneCValidationError("ssrf_host_not_allowed")
            return raw
        return urljoin(self._base_url, raw.lstrip("/"))

    def is_supported(self) -> bool:
        return self._transport in SUPPORTED_TRANSPORTS

    def is_deferred(self) -> bool:
        return self._transport in DEFERRED_TRANSPORTS
