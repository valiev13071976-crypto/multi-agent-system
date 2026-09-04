"""Block 19 stock projection. Reuses marketplace.stock_sync.export_quantity. UNKNOWN ≠ 0."""

from __future__ import annotations

from decimal import Decimal

from marketplace.stock_sync import export_quantity

KIND_KNOWN_ZERO = "KNOWN_ZERO"
KIND_KNOWN_POSITIVE = "KNOWN_POSITIVE"
KIND_UNKNOWN = "UNKNOWN"

FRESH = "FRESH"
STALE = "STALE"
UNKNOWN_FRESHNESS = "UNKNOWN_FRESHNESS"


def classify_quantity(value) -> tuple[str, Decimal | None]:
    if value is None or value == "" or str(value).strip().upper() == "UNKNOWN":
        return KIND_UNKNOWN, None
    qty = Decimal(str(value))
    if qty < 0:
        raise ValueError("negative")
    if qty == 0:
        return KIND_KNOWN_ZERO, qty
    return KIND_KNOWN_POSITIVE, qty


def published_quantity(
    *,
    available,
    reserved=None,
    safety_stock=None,
    freshness: str,
) -> dict:
    kind, avail = classify_quantity(available)
    issues: list[str] = []
    if freshness == STALE:
        return {"kind": kind, "published": None, "decision": "DENY", "code": "STALE_STOCK", "issues": ("stale",)}
    if freshness == UNKNOWN_FRESHNESS:
        return {
            "kind": kind,
            "published": None,
            "decision": "REQUIRE_REVIEW",
            "code": "STALE_STOCK",
            "issues": ("unknown_freshness",),
        }
    if kind == KIND_UNKNOWN:
        return {"kind": kind, "published": None, "decision": "DENY", "code": "UNKNOWN_STOCK", "issues": ("unknown_stock",)}
    assert avail is not None
    res_kind, reserved_q = (KIND_UNKNOWN, None)
    try:
        if reserved is not None and reserved != "":
            res_kind, reserved_q = classify_quantity(reserved)
    except ValueError:
        return {"kind": kind, "published": None, "decision": "DENY", "code": "INVALID_STOCK", "issues": ("reserved_invalid",)}
    if res_kind != KIND_UNKNOWN and reserved_q is not None and reserved_q > avail:
        issues.append("reserved_exceeds_available")
        return {
            "kind": kind,
            "published": None,
            "decision": "REQUIRE_REVIEW",
            "code": "INVALID_STOCK",
            "issues": tuple(issues),
        }
    safety = Decimal("0")
    if safety_stock not in (None, ""):
        safety = Decimal(str(safety_stock))
        if safety < 0:
            return {"kind": kind, "published": None, "decision": "DENY", "code": "INVALID_STOCK", "issues": ("safety_negative",)}
    pub = export_quantity(available=avail, buffer=safety, allow_negative=False)
    decision = "ALLOW"
    if safety > avail:
        issues.append("safety_stock_exceeds_available_floored")
        decision = "WARN"
    return {
        "kind": kind,
        "available": str(avail),
        "safety_stock": str(safety),
        "published": str(pub),
        "decision": decision,
        "code": None,
        "issues": tuple(issues),
    }
