"""Price list comparison and stock reconciliation (no retail pricing / no auto publish)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from data_intel.cleaning import clean_text, normalize_decimal_string
from data_intel.errors import DATASET_CURRENCY_MISMATCH
from data_intel.product_match import match_products


def _dec(value) -> Decimal | None:
    text = normalize_decimal_string(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _product_key(row: dict) -> str:
    for k in ("ean", "mpn", "sku", "article"):
        v = clean_text(row.get(k))
        if v:
            return f"{k}:{v.upper()}"
    name = clean_text(row.get("product_name") or row.get("name") or row.get("товар"))
    return f"name:{(name or '').lower()}"


def _currency(row: dict) -> str:
    return (clean_text(row.get("currency") or row.get("валюта")) or "").upper()


def compare_price_lists(
    left_rows: list[dict],
    right_rows: list[dict],
    *,
    left_supplier: str = "left",
    right_supplier: str = "right",
) -> dict:
    """Compare supplier price lists by matched products."""
    left_map = {_product_key(r): r for r in left_rows}
    right_map = {_product_key(r): r for r in right_rows}
    keys = set(left_map) | set(right_map)

    matched, changed, new, missing, conflicts, unresolved = [], [], [], [], [], []
    for key in sorted(keys):
        l = left_map.get(key)
        r = right_map.get(key)
        if l and r:
            m = match_products(l, r, left_ref=key, right_ref=key)
            if m.conflicts:
                conflicts.append({"key": key, "match": m, "left": l, "right": r})
                continue
            if not m.same_entity and m.confidence in {"low", "unresolved"}:
                unresolved.append({"key": key, "match": m, "left": l, "right": r})
                continue
            lc, rc = _currency(l), _currency(r)
            if lc and rc and lc != rc:
                unresolved.append(
                    {
                        "key": key,
                        "reason": DATASET_CURRENCY_MISMATCH,
                        "left_currency": lc,
                        "right_currency": rc,
                        "left": l,
                        "right": r,
                    }
                )
                continue
            lp = _dec(l.get("price"))
            rp = _dec(r.get("price"))
            row = {
                "supplier_left": left_supplier,
                "supplier_right": right_supplier,
                "key": key,
                "sku": l.get("sku") or r.get("sku"),
                "ean": l.get("ean") or r.get("ean"),
                "mpn": l.get("mpn") or r.get("mpn"),
                "product": l.get("product_name") or l.get("name") or r.get("product_name") or r.get("name"),
                "old_price": str(lp) if lp is not None else None,
                "new_price": str(rp) if rp is not None else None,
                "stock_left": l.get("stock"),
                "stock_right": r.get("stock"),
                "moq_left": l.get("moq"),
                "moq_right": r.get("moq"),
                "source_date_left": l.get("source_date") or l.get("date"),
                "source_date_right": r.get("source_date") or r.get("date"),
                "match_method": m.match_method,
            }
            if lp is not None and rp is not None:
                diff = rp - lp
                row["abs_diff"] = str(diff)
                row["pct_diff"] = str((diff / lp * 100) if lp != 0 else None)
                if diff != 0:
                    changed.append(row)
                else:
                    matched.append(row)
            else:
                matched.append(row)
        elif r and not l:
            new.append({"key": key, "supplier": right_supplier, "row": r})
        elif l and not r:
            missing.append({"key": key, "supplier": left_supplier, "row": l})

    return {
        "summary": {
            "matched": len(matched),
            "changed": len(changed),
            "new": len(new),
            "missing": len(missing),
            "conflicts": len(conflicts),
            "unresolved": len(unresolved),
        },
        "matched": matched,
        "changed": changed,
        "new": new,
        "missing": missing,
        "conflicts": conflicts,
        "unresolved": unresolved,
    }


def reconcile_stock(
    supplier_rows: list[dict],
    internal_rows: list[dict],
    *,
    stale_days: int | None = None,
) -> dict:
    """Compare supplier vs internal/warehouse stock. No automatic publication."""
    s_map = {_product_key(r): r for r in supplier_rows}
    i_map = {_product_key(r): r for r in internal_rows}
    matched, missing, discrepancy, unresolved = [], [], [], []
    stale = []
    for key in sorted(set(s_map) | set(i_map)):
        s = s_map.get(key)
        i = i_map.get(key)
        if s and i:
            ss = _dec(s.get("stock"))
            ii = _dec(i.get("stock") or i.get("warehouse_stock"))
            item = {"key": key, "supplier_stock": str(ss) if ss is not None else None, "internal_stock": str(ii) if ii is not None else None, "product": s.get("product_name") or i.get("product_name")}
            if ss is not None and ii is not None and ss != ii:
                discrepancy.append(item)
            else:
                matched.append(item)
        elif s and not i:
            missing.append({"key": key, "side": "internal", "row": s})
        else:
            unresolved.append({"key": key, "side": "supplier", "row": i})
    return {
        "matched": matched,
        "missing": missing,
        "discrepancy": discrepancy,
        "stale": stale,
        "unresolved": unresolved,
        "stale_days": stale_days,
    }
