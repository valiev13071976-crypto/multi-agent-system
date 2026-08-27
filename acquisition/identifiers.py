"""Deterministic SKU / EAN / GTIN / MPN normalization and validation."""

from __future__ import annotations

import re


_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_WS = re.compile(r"\s+")


def normalize_ean(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    if len(digits) in {8, 12, 13, 14}:
        return digits
    return None


def validate_ean(value: str | None) -> bool:
    """GS1 check-digit validation for EAN-8/12/13/14 (GTIN)."""
    digits = normalize_ean(value)
    if digits is None:
        return False
    body, check = digits[:-1], int(digits[-1])
    total = 0
    # Right-to-left: odd positions ×3, even ×1 (GS1)
    for i, ch in enumerate(reversed(body)):
        n = int(ch)
        total += n * 3 if (i % 2 == 0) else n
    expected = (10 - (total % 10)) % 10
    return expected == check


def normalize_gtin(value: str | None) -> str | None:
    ean = normalize_ean(value)
    if ean is None:
        return None
    return ean.zfill(14)


def normalize_mpn(value: str | None) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    cleaned = _NON_ALNUM.sub("", raw)
    return cleaned or None


def normalize_sku(value: str | None) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    cleaned = _WS.sub("", raw)
    cleaned = cleaned.replace("_", "-")
    return cleaned or None


def normalize_brand(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return _WS.sub(" ", raw)


def normalize_model(value: str | None) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    return _NON_ALNUM.sub(" ", raw).strip()


def normalize_name(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return _WS.sub(" ", raw)


def identifier_bundle(fields: dict) -> dict[str, str]:
    """Extract normalized hard identifiers from a field map."""
    out: dict[str, str] = {}
    ean = normalize_ean(fields.get("ean") or fields.get("gtin") or fields.get("barcode"))
    if ean and validate_ean(ean):
        out["ean"] = ean
        gtin = normalize_gtin(ean)
        if gtin:
            out["gtin"] = gtin
    mpn = normalize_mpn(fields.get("mpn") or fields.get("manufacturer_sku"))
    if mpn:
        out["mpn"] = mpn
    sku = normalize_sku(fields.get("sku") or fields.get("supplier_sku") or fields.get("source_sku"))
    if sku:
        out["sku"] = sku
    brand = normalize_brand(fields.get("brand"))
    if brand:
        out["brand"] = brand
    model = normalize_model(fields.get("model") or fields.get("name"))
    if model:
        out["model"] = model
    return out
