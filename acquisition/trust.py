"""Source trust helpers — evidence quality, not Tool security trust."""

from __future__ import annotations

from acquisition.models import (
    SOURCE_TRUST_LEVELS,
    TRUST_CONTRACTED_SUPPLIER,
    TRUST_GENERAL_WEB,
    TRUST_KNOWN_RETAILER,
    TRUST_MARKETPLACE_API,
    TRUST_OFFICIAL_MANUFACTURER,
    TRUST_UNKNOWN,
)

# Coarse evidence weight for confidence blending (not authorization)
TRUST_WEIGHTS = {
    TRUST_OFFICIAL_MANUFACTURER: 1.0,
    TRUST_CONTRACTED_SUPPLIER: 0.9,
    TRUST_MARKETPLACE_API: 0.75,
    TRUST_KNOWN_RETAILER: 0.65,
    TRUST_GENERAL_WEB: 0.4,
    TRUST_UNKNOWN: 0.2,
}


def trust_weight(level: str) -> float:
    return float(TRUST_WEIGHTS.get(level, TRUST_WEIGHTS[TRUST_UNKNOWN]))


def is_valid_trust(level: str) -> bool:
    return level in SOURCE_TRUST_LEVELS


def default_trust_for_source_type(source_type: str) -> str:
    mapping = {
        "manufacturer": TRUST_OFFICIAL_MANUFACTURER,
        "supplier": TRUST_CONTRACTED_SUPPLIER,
        "marketplace": TRUST_MARKETPLACE_API,
        "competitor": TRUST_KNOWN_RETAILER,
        "website": TRUST_GENERAL_WEB,
        "search": TRUST_GENERAL_WEB,
        "api": TRUST_MARKETPLACE_API,
        "document": TRUST_CONTRACTED_SUPPLIER,
        "feed": TRUST_CONTRACTED_SUPPLIER,
    }
    return mapping.get(source_type, TRUST_UNKNOWN)
