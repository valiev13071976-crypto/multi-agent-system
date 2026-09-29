"""Configurable catalog research scope; never infer manufacture from market."""
import json
import os
import re
from urllib.parse import urlparse


def catalog_market(tenant_id: str) -> str:
    overrides = json.loads(os.environ.get("PANDA_CATALOG_RESEARCH_MARKETS") or "{}")
    value = overrides.get(tenant_id, os.environ.get("PANDA_CATALOG_RESEARCH_MARKET", ""))
    market = str(value or "").strip().upper()
    if market not in {"", "RU"}:
        raise ValueError("unsupported_catalog_research_market")
    return market


def russian_source(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host.endswith((".ru", ".xn--p1ai")):
        return True
    # Localized global sites and their regional document libraries.
    return bool(re.match(r"^/ru(?:/|$)", path)
                or re.search(r"/region/(?:russia|ru)(?:/|$)", path))


def requests_fresh_research(text: str) -> bool:
    return bool(re.search(r"заново|повторно|перепроверь|обновл[её]н|не повторяй стар|refresh|recheck",
                          str(text or ""), re.I))
