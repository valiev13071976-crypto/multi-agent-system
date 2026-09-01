"""CommerceML boundary — safe XML parsing, deferred for automatic WRITE."""

from __future__ import annotations

import xml.etree.ElementTree as ET

MAX_COMMERCEML_BYTES = 2_097_152


def parse_commerceml_safe(content: bytes) -> dict:
    """Bounded safe CommerceML parse — readiness boundary only."""
    if len(content) > MAX_COMMERCEML_BYTES:
        raise ValueError("commerceml_payload_too_large")
    text = content.decode("utf-8", errors="replace")
    if "<!ENTITY" in text.upper() or "<!DOCTYPE" in text.upper():
        raise ValueError("commerceml_external_entities_forbidden")
    root = ET.fromstring(content)
    offers = []
    for offer in root.iter():
        tag = offer.tag.split("}")[-1] if "}" in offer.tag else offer.tag
        if tag.lower() in {"offer", "product"}:
            offers.append(
                {
                    "id": offer.get("id") or "",
                    "name": (offer.findtext(".//{*}Name") or offer.findtext("Name") or "").strip(),
                    "article": (offer.findtext(".//{*}Article") or offer.findtext("Article") or "").strip(),
                }
            )
    return {"format": "commerceml", "offers": offers[:1000], "bounded": True, "write": "DEFERRED"}
