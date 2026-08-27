"""Dataset search, aggregation, and pivot-style reports."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from data_intel.cleaning import clean_text, normalize_decimal_string
from data_intel.counterparty import normalize_legal_name
from data_intel.identifiers_ru import normalize_inn


def _dec(value) -> Decimal | None:
    text = normalize_decimal_string(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def search_rows(
    rows: list[dict],
    *,
    inn: str | None = None,
    company_name: str | None = None,
    sku: str | None = None,
    ean: str | None = None,
    article: str | None = None,
    document_number: str | None = None,
    amount: str | None = None,
    date: str | None = None,
    filters: dict | None = None,
    fuzzy_name: bool = False,
    sort_by: str | None = None,
    sort_desc: bool = False,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """Search without Excel formulas. Exact + normalized + optional fuzzy name."""
    filters = dict(filters or {})
    want_inn = normalize_inn(inn).normalized if inn else None
    want_name = normalize_legal_name(company_name) if company_name else None
    want_sku = (clean_text(sku) or "").upper() or None
    want_ean = (clean_text(ean) or "").upper() or None
    want_article = (clean_text(article) or "").upper() or None
    want_doc = clean_text(document_number)
    want_amount = normalize_decimal_string(amount) if amount else None
    want_date = clean_text(date)

    hits = []
    for i, row in enumerate(rows):
        if want_inn:
            r_inn = normalize_inn(row.get("inn"))
            if not (r_inn.valid and r_inn.normalized == want_inn):
                continue
        if want_name:
            r_name = normalize_legal_name(
                row.get("company_name") or row.get("counterparty") or row.get("name")
            )
            if not r_name:
                continue
            if fuzzy_name:
                ta, tb = set(want_name.split()), set(r_name.split())
                if not ta or len(ta & tb) / len(ta | tb) < 0.6:
                    continue
            elif r_name != want_name and want_name not in r_name and r_name not in want_name:
                continue
        if want_sku and (clean_text(row.get("sku")) or "").upper() != want_sku:
            continue
        if want_ean and (clean_text(row.get("ean")) or "").upper() != want_ean:
            continue
        if want_article and (clean_text(row.get("article")) or "").upper() != want_article:
            continue
        if want_doc and clean_text(row.get("document_number")) != want_doc:
            continue
        if want_amount and normalize_decimal_string(row.get("amount")) != want_amount:
            continue
        if want_date and clean_text(row.get("payment_date") or row.get("date")) != want_date:
            continue
        ok = True
        for fk, fv in filters.items():
            if clean_text(row.get(fk)) != clean_text(fv):
                ok = False
                break
        if ok:
            hits.append({"index": i, "row": row})

    if sort_by:
        hits.sort(key=lambda h: str(h["row"].get(sort_by) or ""), reverse=sort_desc)

    total = len(hits)
    page = hits[offset : offset + max(1, limit)]
    return {"total": total, "offset": offset, "limit": limit, "hits": page}


def aggregate(
    rows: list[dict],
    *,
    group_by: list[str] | None = None,
    measures: dict[str, str] | None = None,
) -> list[dict]:
    """measures: {field: sum|count|min|max|avg|distinct}."""
    group_by = group_by or []
    measures = measures or {"_rows": "count"}
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = tuple(clean_text(row.get(g)) or "" for g in group_by) if group_by else ()
        buckets[key].append(row)

    out = []
    for key, group in buckets.items():
        item = {g: key[i] for i, g in enumerate(group_by)}
        for field, op in measures.items():
            op = op.lower()
            if op == "count":
                item[f"{field}_count" if field != "_rows" else "count"] = len(group)
            elif op == "distinct":
                item[f"{field}_distinct"] = len({clean_text(r.get(field)) for r in group})
            else:
                vals = [_dec(r.get(field)) for r in group]
                vals = [v for v in vals if v is not None]
                if op == "sum":
                    item[f"{field}_sum"] = str(sum(vals, Decimal("0")))
                elif op == "min" and vals:
                    item[f"{field}_min"] = str(min(vals))
                elif op == "max" and vals:
                    item[f"{field}_max"] = str(max(vals))
                elif op == "avg" and vals:
                    item[f"{field}_avg"] = str(sum(vals, Decimal("0")) / len(vals))
        out.append(item)
    return out


def pivot_report(
    rows: list[dict],
    *,
    row_fields: list[str],
    column_fields: list[str] | None = None,
    measure_field: str = "amount",
    measure_op: str = "sum",
    filters: dict | None = None,
) -> dict:
    """Pivot-style representation without Excel PivotCache."""
    filtered = rows
    if filters:
        filtered = [
            r
            for r in rows
            if all(clean_text(r.get(k)) == clean_text(v) for k, v in filters.items())
        ]
    column_fields = column_fields or []
    col_values = sorted(
        {
            "|".join(str(clean_text(r.get(c)) or "") for c in column_fields)
            for r in filtered
        }
    ) if column_fields else [""]

    # group
    groups: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in filtered:
        rk = tuple(clean_text(r.get(f)) or "" for f in row_fields)
        ck = "|".join(str(clean_text(r.get(c)) or "") for c in column_fields) if column_fields else ""
        groups[rk][ck].append(r)

    matrix = []
    for rk, cols in groups.items():
        item = {row_fields[i]: rk[i] for i in range(len(row_fields))}
        row_total = Decimal("0")
        for cv in col_values:
            vals = [_dec(x.get(measure_field)) for x in cols.get(cv, [])]
            vals = [v for v in vals if v is not None]
            if measure_op == "count":
                cell = len(cols.get(cv, []))
            elif measure_op == "sum":
                cell = sum(vals, Decimal("0"))
            else:
                cell = sum(vals, Decimal("0"))
            key = cv or measure_field
            item[key] = str(cell)
            if isinstance(cell, Decimal):
                row_total += cell
            else:
                row_total += Decimal(cell)
        item["total"] = str(row_total)
        matrix.append(item)

    return {
        "row_fields": row_fields,
        "column_fields": column_fields,
        "columns": col_values,
        "measure": measure_field,
        "op": measure_op,
        "rows": matrix,
    }
