"""Tenant access and URL normalization for SEO."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from seo_marketing.errors import SEO_ACCESS_DENIED, SEO_INVALID_URL, SEO_PROPERTY_DENIED, SEO_SOURCE_DENIED, SeoMarketingError

_PRIVATE_HOSTS = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|0\.0\.0\.0|\[::1\])",
    re.I,
)


class SeoAccessPolicy:
    def require(self, *, requesting_tenant: str, target_tenant: str) -> None:
        if requesting_tenant != target_tenant:
            raise SeoMarketingError(SEO_ACCESS_DENIED)

    def require_property(self, *, tenant_id: str, site_property: str, bound_property: str) -> None:
        if not bound_property or site_property != bound_property:
            raise SeoMarketingError(SEO_PROPERTY_DENIED)

    def require_analytics_property(self, *, tenant_id: str, requested: str, bound: str) -> None:
        if not bound or requested != bound:
            raise SeoMarketingError(SEO_PROPERTY_DENIED)


def normalize_url(url: str, *, trailing_slash: bool = False) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise SeoMarketingError(SEO_INVALID_URL)
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host or _PRIVATE_HOSTS.match(host):
        raise SeoMarketingError(SEO_SOURCE_DENIED)
    scheme = (parsed.scheme or "https").lower()
    port = parsed.port
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if trailing_slash and not path.endswith("/"):
        path = path + "/"
    if not trailing_slash and path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))
