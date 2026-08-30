"""Provider error classification helpers (rate-limit / retry-after)."""

from __future__ import annotations


def is_rate_limit_error(exc) -> bool:
    """True when the exception indicates a provider rate limit (HTTP 429)."""

    if exc is None:
        return False
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    try:
        if int(status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    name = type(exc).__name__
    if "RateLimit" in name:
        return True
    code = str(
        getattr(exc, "error_code", None)
        or getattr(exc, "code", None)
        or getattr(exc, "reason", None)
        or ""
    ).strip().lower()
    if code in {"provider_rate_limit", "rate_limit", "rate_limit_exceeded"}:
        return True
    if "provider_rate_limit" in str(exc).lower():
        return True
    if "429" in str(exc):
        return True
    return False


def parse_retry_after_from_exc(exc) -> float | None:
    """Extract Retry-After seconds from a provider exception, if present."""

    if exc is None:
        return None
    from providers.governor import parse_retry_after

    headers = getattr(exc, "headers", None) or {}
    if isinstance(headers, dict):
        value = headers.get("Retry-After") or headers.get("retry-after")
        parsed = parse_retry_after(value)
        if parsed is not None:
            return parsed
    for attr in ("retry_after", "retry_after_seconds"):
        parsed = parse_retry_after(getattr(exc, attr, None))
        if parsed is not None:
            return parsed
    return None
