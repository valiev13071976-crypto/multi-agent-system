"""Block 10 business process orchestration — reuses data_intel primitives (not a second engine)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from data_intel.analysis import analyze_margin
from data_intel.cleaning import clean_text, normalize_decimal_string
from data_intel.compare import reconcile_stock
from data_intel.contracts import utc_now
from data_intel.economics import economics_batch_rows, economics_result_table
from data_intel.excel_out import generate_business_result_workbook, generate_economics_workbook
from data_intel.merge import merge_datasets
from data_intel.product_match import match_products
from data_intel.quality import build_quality_report, mapping_gate


MATCH_EXACT = "EXACT"
MATCH_NORMALIZED_EXACT = "NORMALIZED_EXACT"
MATCH_AMBIGUOUS = "AMBIGUOUS"
MATCH_UNMATCHED = "UNMATCHED"
MATCH_CONFLICT = "CONFLICT"

INSUFFICIENT_INPUT = "INSUFFICIENT_INPUT"


def _dec(value) -> Decimal | None:
    text = normalize_decimal_string(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _id_value(row: dict, prefer: tuple[str, ...] = ("sku", "article", "ean", "barcode")) -> str:
    for k in prefer:
        v = clean_text(row.get(k))
        if v:
            return v
    return ""


def classify_product_match(left: dict, right: dict) -> dict:
    m = match_products(left, right)
    if m.conflicts:
        state = MATCH_CONFLICT
    elif m.same_entity and m.confidence == "exact":
        state = MATCH_EXACT
    elif m.same_entity and m.confidence in {"exact", "high"}:
        state = MATCH_NORMALIZED_EXACT if m.match_method and "norm" in str(m.match_method).lower() else MATCH_EXACT
    elif m.same_entity and m.confidence in {"medium"}:
        state = MATCH_AMBIGUOUS
    elif not m.same_entity:
        state = MATCH_UNMATCHED
    else:
        state = MATCH_AMBIGUOUS
    return {
        "state": state,
        "confidence": m.confidence,
        "method": m.match_method,
        "review_required": m.review_required,
        "conflicts": list(m.conflicts),
        "evidence": dict(m.evidence),
    }


def find_conflicting_identifier_duplicates(
    rows: list[dict],
    *,
    identifier_keys: list[str] | None = None,
    value_keys: list[str] | None = None,
) -> list[dict]:
    """Same identifier + different business values → conflicting duplicate (never silent discard)."""
    from collections import defaultdict

    id_keys = identifier_keys or ["sku", "article", "ean"]
    val_keys = value_keys or ["price", "selling_price", "purchase_price", "stock", "product_name"]
    by_id: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        for k in id_keys:
            v = clean_text(row.get(k))
            if v:
                by_id[f"{k}:{v.upper()}"].append(i)
                break
    conflicts = []
    identical = []
    for key, idxs in by_id.items():
        if len(idxs) < 2:
            continue
        fingerprints = []
        for i in idxs:
            fp = tuple(clean_text(rows[i].get(vk)) or "" for vk in val_keys)
            fingerprints.append(fp)
        if len(set(fingerprints)) == 1:
            identical.append({"kind": "identical_duplicate", "key": key, "indices": idxs})
        else:
            conflicts.append(
                {
                    "kind": "conflicting_duplicate",
                    "key": key,
                    "indices": idxs,
                    "values": [
                        {vk: rows[i].get(vk) for vk in val_keys}
                        | {"__source_row": rows[i].get("__source_row"), "__row_ref": rows[i].get("__row_ref")}
                        for i in idxs
                    ],
                }
            )
    return identical + conflicts


def basic_margin(row: dict) -> dict:
    """Deterministic spreadsheet arithmetic only. Never labeled net profit."""
    result = analyze_margin(row)
    if result.get("unresolved"):
        return {
            "status": INSUFFICIENT_INPUT,
            "absolute_difference": None,
            "margin_pct_of_selling": None,
            "formula": "selling_price - purchase_price",
            "missing": list(result["unresolved"]),
            "note": "Not net profit; marketplace/tax/logistics excluded (Block 11+)",
        }
    return {
        "status": "OK",
        "absolute_difference": result.get("absolute_margin"),
        "margin_pct_of_selling": result.get("margin_pct"),
        "markup_pct_of_purchase": result.get("markup_pct"),
        "formula": "selling_price - purchase_price",
        "missing": [],
        "note": "Gross difference only — not net profit",
    }


def stock_reconciliation_report(left_rows: list[dict], right_rows: list[dict]) -> dict:
    raw = reconcile_stock(left_rows, right_rows)
    rows = []
    for item in raw.get("matched") or []:
        a = _dec(item.get("supplier_stock"))
        b = _dec(item.get("internal_stock"))
        diff = (a - b) if a is not None and b is not None else None
        rows.append(
            {
                "identifier": item.get("key"),
                "product": item.get("product"),
                "stock_A": item.get("supplier_stock"),
                "stock_B": item.get("internal_stock"),
                "difference": str(diff) if diff is not None else None,
                "status": "OK" if diff == 0 else "MATCHED",
            }
        )
    for item in raw.get("discrepancy") or []:
        a = _dec(item.get("supplier_stock"))
        b = _dec(item.get("internal_stock"))
        diff = (a - b) if a is not None and b is not None else None
        status = "NEGATIVE" if (a is not None and a < 0) or (b is not None and b < 0) else "DIFF"
        rows.append(
            {
                "identifier": item.get("key"),
                "product": item.get("product"),
                "stock_A": item.get("supplier_stock"),
                "stock_B": item.get("internal_stock"),
                "difference": str(diff) if diff is not None else None,
                "status": status,
            }
        )
    for item in raw.get("missing") or []:
        rows.append(
            {
                "identifier": item.get("key"),
                "product": (item.get("row") or {}).get("product_name"),
                "stock_A": (item.get("row") or {}).get("stock"),
                "stock_B": None,
                "difference": None,
                "status": "MISSING_IN_B",
            }
        )
    for item in raw.get("unresolved") or []:
        rows.append(
            {
                "identifier": item.get("key"),
                "product": (item.get("row") or {}).get("product_name"),
                "stock_A": None,
                "stock_B": (item.get("row") or {}).get("stock") or (item.get("row") or {}).get("warehouse_stock"),
                "difference": None,
                "status": "MISSING_IN_A",
            }
        )
    return {"rows": rows, "raw": raw, "summary": {"rows": len(rows)}}


def price_comparison_changed_only(
    left_rows: list[dict],
    right_rows: list[dict],
    *,
    left_supplier: str = "A",
    right_supplier: str = "B",
    match_on: tuple[str, ...] = ("article", "sku", "ean"),
) -> dict:
    """Price list A vs B; RESULT focused on changed prices. Match by configured identifier."""

    def _prep(rows):
        out = []
        for r in rows:
            nr = dict(r)
            if nr.get("price") is None:
                nr["price"] = nr.get("selling_price") or nr.get("purchase_price")
            if nr.get("sku") is None and nr.get("article"):
                nr["sku"] = nr["article"]
            if nr.get("article") is None and nr.get("sku"):
                nr["article"] = nr["sku"]
            out.append(nr)
        return out

    def _key(row: dict) -> str:
        for k in match_on:
            v = clean_text(row.get(k))
            if v:
                return f"{k}:{v.upper()}"
        return _product_key_fallback(row)

    left_p = _prep(left_rows)
    right_p = _prep(right_rows)
    # Use article/sku-first maps instead of ean-first default in compare_price_lists
    from decimal import Decimal, InvalidOperation
    from data_intel.product_match import match_products

    def _d(value):
        text = normalize_decimal_string(value)
        if text is None:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None

    left_map = {_key(r): r for r in left_p}
    right_map = {_key(r): r for r in right_p}
    matched, changed, new, missing, conflicts, unresolved = [], [], [], [], [], []
    for key in sorted(set(left_map) | set(right_map)):
        l = left_map.get(key)
        r = right_map.get(key)
        if l and r:
            m = match_products(l, r, left_ref=key, right_ref=key)
            if m.conflicts:
                conflicts.append({"key": key, "match": m})
                continue
            lp, rp = _d(l.get("price")), _d(r.get("price"))
            row = {
                "key": key,
                "sku": l.get("sku") or r.get("sku"),
                "ean": l.get("ean") or r.get("ean"),
                "product": l.get("product_name") or l.get("name") or r.get("product_name") or r.get("name"),
                "old_price": str(lp) if lp is not None else None,
                "new_price": str(rp) if rp is not None else None,
                "match_method": m.match_method or MATCH_EXACT,
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

    cmp = {
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
    changed_out = []
    for row in changed:
        changed_out.append(
            {
                "identifier": row.get("key"),
                "product": row.get("product"),
                "old_price": row.get("old_price"),
                "new_price": row.get("new_price"),
                "absolute_difference": row.get("abs_diff"),
                "percentage_difference": row.get("pct_diff"),
                "match_status": row.get("match_method") or MATCH_EXACT,
                "sku": row.get("sku"),
                "ean": row.get("ean"),
                "warnings": "",
            }
        )
    return {"changed": changed_out, "comparison": cmp, "summary": dict(cmp["summary"])}


def _product_key_fallback(row: dict) -> str:
    name = clean_text(row.get("product_name") or row.get("name") or "")
    return f"name:{(name or '').lower()}"


def merge_with_provenance(
    left_rows: list[dict],
    right_rows: list[dict],
    *,
    left_file: str = "A",
    right_file: str = "B",
    left_sheet: str = "",
    right_sheet: str = "",
) -> dict:
    left = []
    for r in left_rows:
        nr = dict(r)
        nr.setdefault("source_file", left_file)
        nr.setdefault("source_sheet", left_sheet or r.get("__source_sheet") or "")
        nr.setdefault("source_row", r.get("__source_row"))
        left.append(nr)
    right = []
    for r in right_rows:
        nr = dict(r)
        nr.setdefault("source_file", right_file)
        nr.setdefault("source_sheet", right_sheet or r.get("__source_sheet") or "")
        nr.setdefault("source_row", r.get("__source_row"))
        right.append(nr)
    merged = merge_datasets(left, right, keys=[], how="append")
    # Remove exact duplicates by fingerprint of business fields excluding provenance
    rows = merged["rows"]
    seen = set()
    unique = []
    exact_dups = 0
    for r in rows:
        keys = ("sku", "article", "ean", "product_name", "price", "selling_price", "purchase_price", "stock")
        fp = tuple(clean_text(r.get(k)) or "" for k in keys)
        if fp in seen and any(fp):
            exact_dups += 1
            continue
        seen.add(fp)
        unique.append(r)
    conflicts = find_conflicting_identifier_duplicates(unique)
    return {
        "rows": unique,
        "exact_duplicates_removed": exact_dups,
        "conflicts": [c for c in conflicts if c.get("kind") == "conflicting_duplicate"],
        "summary": {"rows": len(unique), "exact_duplicates_removed": exact_dups, "conflicts": len([c for c in conflicts if c.get("kind") == "conflicting_duplicate"])},
    }


def build_business_workbook(
    *,
    result_headers: list[str],
    result_rows: list[list],
    issues: list[dict],
    summary: dict,
    provenance: dict | None = None,
    text_cols: set[int] | None = None,
) -> bytes:
    issue_headers = ["file", "sheet", "row", "column", "reason", "severity"]
    issue_body = [
        [
            i.get("file", ""),
            i.get("sheet", ""),
            i.get("row", ""),
            i.get("column", ""),
            i.get("reason", ""),
            i.get("severity", ""),
        ]
        for i in issues
    ]
    meta = dict(summary or {})
    meta.setdefault("generated_at", utc_now().isoformat())
    return generate_business_result_workbook(
        result_headers=result_headers,
        result_rows=result_rows,
        issue_headers=issue_headers,
        issue_rows=issue_body,
        summary=meta,
        provenance=provenance or {},
        text_cols=text_cols,
    )


def run_economics_batch(
    rows: list[dict],
    *,
    policy=None,
    channel: str = "SITE",
    channel_configs: dict | None = None,
) -> dict:
    """Block 11 batch economics — reuses Block 10 row normalization, not a second Excel engine."""
    batch = economics_batch_rows(
        rows,
        policy=policy,
        channel=channel,
        channel_configs=channel_configs,
    )
    headers, body, text_cols = economics_result_table(batch["results"])
    content = generate_economics_workbook(
        economics_headers=headers,
        economics_rows=body,
        issues=batch["issues"],
        summary=batch["summary"],
        scenarios=batch.get("scenarios") or [],
        text_cols=text_cols,
        provenance={"process": "product_economics", "original_preserved": True},
    )
    return {
        "status": "OK",
        "content": content,
        "size": len(content),
        "summary": batch["summary"],
        "results": batch["results"],
        "issues": batch["issues"],
    }


def assess_structure_for_process(service, dataset_id: str, *, tenant_id: str, required_roles: set[str] | None = None) -> dict:
    desc = service.store.get_dataset(dataset_id, tenant_id=tenant_id)
    if desc is None:
        from data_intel.errors import DATASET_ACCESS_DENIED, DataIntelError

        raise DataIntelError(DATASET_ACCESS_DENIED)
    table = desc.tables[0] if desc.tables else None
    cols = table.columns if table else ()
    gate = mapping_gate(cols, required_roles=required_roles)
    return {
        "dataset_id": dataset_id,
        "sheet": table.sheet if table else "",
        "dimensions": {"rows": desc.row_count, "columns": desc.column_count},
        "header_row": table.header_row if table else None,
        "unresolved": table.unresolved if table else True,
        "mapping": gate,
        "status": gate["status"],
    }
