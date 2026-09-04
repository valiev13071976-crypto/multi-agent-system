"""Product-line mapping — no guessing. Reuses identity keys from Blocks 12–14."""

from __future__ import annotations

from order_orchestration.contracts import AMBIGUOUS, MAPPED, MISSING, OrderLine


def resolve_line(line: OrderLine, *, catalog: dict[str, str | list[str]]) -> OrderLine:
    keys = [k for k in (line.product_id, line.sku, line.article, line.barcode, line.source_offer_id) if k]
    hits: list[str] = []
    for key in keys:
        val = catalog.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            hits.extend(str(x) for x in val)
        else:
            hits.append(str(val))
    uniq = list(dict.fromkeys(hits))
    if len(uniq) > 1:
        return OrderLine(**{**line.__dict__, "mapping_status": AMBIGUOUS, "mapping_product_id": ""})
    if len(uniq) == 1:
        return OrderLine(**{**line.__dict__, "mapping_status": MAPPED, "mapping_product_id": uniq[0]})
    if line.product_id and not catalog:
        return OrderLine(**{**line.__dict__, "mapping_status": MAPPED, "mapping_product_id": line.product_id})
    return OrderLine(**{**line.__dict__, "mapping_status": MISSING, "mapping_product_id": ""})
