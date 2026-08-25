from datetime import datetime, timezone
import os

from tools.models import (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_LEVELS,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    TRUST_MEDIUM,
    TRUST_UNKNOWN,
)

SEARCH_TOOL_TRUST = TOOL_TRUST_READ_ONLY_EXTERNAL


DEFAULT_TRUSTED_DOMAINS = frozenset(
    {
        "wikipedia.org",
        "who.int",
        "un.org",
        "europa.eu",
        "nih.gov",
        "cdc.gov",
        "census.gov",
        "oecd.org",
        "nature.com",
        "science.org",
    }
)


def load_trusted_domains() -> frozenset[str]:
    raw = os.getenv("FACT_TRUSTED_DOMAINS")
    if not raw or not str(raw).strip():
        return DEFAULT_TRUSTED_DOMAINS
    items = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return frozenset(items) or DEFAULT_TRUSTED_DOMAINS


def trust_for_domain(domain: str, trusted: frozenset[str] | None = None) -> str:
    host = (domain or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    allowed = trusted if trusted is not None else load_trusted_domains()
    for suffix in allowed:
        if host == suffix or host.endswith("." + suffix):
            return TRUST_MEDIUM
    return TRUST_UNKNOWN


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
