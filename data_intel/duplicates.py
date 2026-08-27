"""Duplicate detection — exact and near-duplicate groups; no auto-delete."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from data_intel.cleaning import clean_text
from data_intel.contracts import CONF_EXACT, CONF_HIGH, CONF_MEDIUM
from data_intel.counterparty import normalize_legal_name
from data_intel.identifiers_ru import normalize_inn


def _row_fingerprint(row: dict, keys: list[str] | None = None) -> str:
    if keys:
        parts = [f"{k}={clean_text(row.get(k)) or ''}" for k in keys]
    else:
        items = sorted((k, clean_text(v) or "") for k, v in row.items() if not str(k).startswith("__"))
        parts = [f"{k}={v}" for k, v in items]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def find_duplicates(
    rows: list[dict],
    *,
    business_keys: list[str] | None = None,
) -> list[dict]:
    """Return duplicate groups with evidence. Does not delete."""
    groups: list[dict] = []

    # Exact full-row
    by_fp: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_fp[_row_fingerprint(row)].append(i)
    for fp, idxs in by_fp.items():
        if len(idxs) > 1:
            groups.append(
                {
                    "kind": "exact_row",
                    "confidence": CONF_EXACT,
                    "indices": idxs,
                    "evidence": {"fingerprint": fp},
                }
            )

    # Business key
    if business_keys:
        by_bk: dict[str, list[int]] = defaultdict(list)
        for i, row in enumerate(rows):
            by_bk[_row_fingerprint(row, business_keys)].append(i)
        for fp, idxs in by_bk.items():
            if len(idxs) > 1:
                groups.append(
                    {
                        "kind": "business_key",
                        "confidence": CONF_HIGH,
                        "indices": idxs,
                        "keys": list(business_keys),
                        "evidence": {"fingerprint": fp},
                    }
                )

    # Near: same INN + amount + date/document
    near_keys = []
    sample = rows[0] if rows else {}
    for cand in (("inn", "amount", "payment_date"), ("inn", "amount", "document_number"), ("ean", "sku", "supplier")):
        if all(any(k in r for r in rows[:5]) or k in sample for k in cand):
            near_keys.append(cand)
    for keys in near_keys:
        by_near: dict[str, list[int]] = defaultdict(list)
        for i, row in enumerate(rows):
            vals = []
            for k in keys:
                v = row.get(k)
                if k == "inn":
                    n = normalize_inn(v)
                    v = n.normalized if n.valid else clean_text(v)
                else:
                    v = clean_text(v)
                vals.append(str(v or ""))
            if any(vals):
                by_near["|".join(vals)].append(i)
        for key, idxs in by_near.items():
            if len(idxs) > 1:
                groups.append(
                    {
                        "kind": "near_duplicate",
                        "confidence": CONF_MEDIUM,
                        "indices": idxs,
                        "keys": list(keys),
                        "evidence": {"key": key},
                    }
                )

    # Normalized company name duplicates
    by_name: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        name = normalize_legal_name(row.get("company_name") or row.get("counterparty") or row.get("name"))
        if name:
            by_name[name].append(i)
    for name, idxs in by_name.items():
        if len(idxs) > 1:
            groups.append(
                {
                    "kind": "normalized_company_name",
                    "confidence": CONF_MEDIUM,
                    "indices": idxs,
                    "evidence": {"name": name},
                    "review_required": True,
                }
            )
    return groups
