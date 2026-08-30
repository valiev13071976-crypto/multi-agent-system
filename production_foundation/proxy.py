"""Trusted public URL and proxy semantics."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from production_foundation.config import is_valid_public_url, resolve_production_config


def trusted_public_origin(env: dict | None = None) -> str:
    cfg = resolve_production_config(env)
    url = cfg.public_url.rstrip("/")
    if not is_valid_public_url(url):
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def absolute_url(path: str, *, env: dict | None = None, forwarded_host: str | None = None) -> str:
    """Generate security-sensitive URLs from configured public origin only."""

    origin = trusted_public_origin(env)
    if not origin:
        return path
    if forwarded_host and forwarded_host != urlparse(origin).netloc:
        pass
    return urljoin(origin + "/", path.lstrip("/"))


def external_scheme(*, env: dict | None = None, forwarded_proto: str | None = None) -> str:
    origin = trusted_public_origin(env)
    if origin:
        return urlparse(origin).scheme
    if forwarded_proto in {"http", "https"}:
        return forwarded_proto
    return "http"


def is_secure_request(*, env: dict | None = None, forwarded_proto: str | None = None) -> bool:
    return external_scheme(env=env, forwarded_proto=forwarded_proto) == "https"
