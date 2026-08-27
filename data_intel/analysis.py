"""Margin analysis and explainable anomaly detection."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from statistics import mean, pstdev

from data_intel.cleaning import normalize_decimal_string, normalize_date
from data_intel.contracts import DataIssue, SEVERITY_ERROR, SEVERITY_WARNING


def _dec(value) -> Decimal | None:
    text = normalize_decimal_string(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def analyze_margin(row: dict) -> dict:
    """Calculation only. Missing costs → explicitly unresolved."""
    purchase = _dec(row.get("purchase_price") or row.get("cost"))
    selling = _dec(row.get("selling_price") or row.get("price"))
    unresolved = []
    if purchase is None:
        unresolved.append("purchase_price")
    if selling is None:
        unresolved.append("selling_price")
    if unresolved:
        return {
            "absolute_margin": None,
            "margin_pct": None,
            "markup_pct": None,
            "contribution_value": None,
            "unresolved": unresolved,
        }
    costs = purchase
    included = {"purchase": str(purchase)}
    for key in ("fees", "logistics", "other_costs"):
        if key in row:
            val = _dec(row.get(key))
            if val is None:
                unresolved.append(key)
            else:
                costs = costs + val
                included[key] = str(val)
    if unresolved:
        return {
            "absolute_margin": None,
            "margin_pct": None,
            "markup_pct": None,
            "contribution_value": None,
            "unresolved": unresolved,
            "costs_included": included,
        }
    abs_margin = selling - costs
    margin_pct = (abs_margin / selling * 100) if selling != 0 else None
    markup_pct = (abs_margin / purchase * 100) if purchase != 0 else None
    return {
        "absolute_margin": str(abs_margin),
        "margin_pct": str(margin_pct) if margin_pct is not None else None,
        "markup_pct": str(markup_pct) if markup_pct is not None else None,
        "contribution_value": str(abs_margin),
        "unresolved": [],
        "costs_included": included,
    }


def detect_anomalies(rows: list[dict], *, row_refs: list[str] | None = None) -> list[DataIssue]:
    """Deterministic / statistical anomalies with explainable reasons."""
    issues: list[DataIssue] = []
    refs = row_refs or [f"r{i}" for i in range(len(rows))]
    prices = [_dec(r.get("price") or r.get("selling_price")) for r in rows]
    valid_prices = [p for p in prices if p is not None]
    if len(valid_prices) >= 5:
        vals = [float(p) for p in valid_prices]
        mu = mean(vals)
        sd = pstdev(vals) or 1.0
        for i, p in enumerate(prices):
            if p is None:
                continue
            z = (float(p) - mu) / sd
            if abs(z) >= 3:
                issues.append(
                    DataIssue(
                        row_ref=refs[i],
                        column="price",
                        issue_type="price_spike_drop",
                        severity=SEVERITY_WARNING,
                        description=f"Price z-score={z:.2f} vs mean={mu:.2f}",
                        suggested_action="review_price",
                        evidence={"z": z, "mean": mu},
                    )
                )

    for i, row in enumerate(rows):
        ref = refs[i]
        stock = _dec(row.get("stock"))
        if stock is not None and stock < 0:
            issues.append(
                DataIssue(
                    row_ref=ref,
                    column="stock",
                    issue_type="negative_stock",
                    severity=SEVERITY_ERROR,
                    description="Negative stock",
                    suggested_action="correct_stock",
                )
            )
        d = normalize_date(row.get("payment_date") or row.get("date"))
        if d and d.startswith("19") is False and d < "1990-01-01":
            issues.append(
                DataIssue(
                    row_ref=ref,
                    column="date",
                    issue_type="impossible_date",
                    severity=SEVERITY_ERROR,
                    description=f"Impossible date {d}",
                    suggested_action="correct_date",
                )
            )
        # future far dates
        if d and d > "2100-01-01":
            issues.append(
                DataIssue(
                    row_ref=ref,
                    column="date",
                    issue_type="impossible_date",
                    severity=SEVERITY_ERROR,
                    description=f"Impossible date {d}",
                    suggested_action="correct_date",
                )
            )
        qty = _dec(row.get("quantity"))
        if qty is not None and len(rows) >= 5:
            qtys = [float(q) for q in (_dec(r.get("quantity")) for r in rows) if q is not None]
            if qtys:
                qmu = mean(qtys)
                qsd = pstdev(qtys) or 1.0
                if abs(float(qty) - qmu) / qsd >= 3:
                    issues.append(
                        DataIssue(
                            row_ref=ref,
                            column="quantity",
                            issue_type="quantity_outlier",
                            severity=SEVERITY_WARNING,
                            description="Quantity outlier",
                            suggested_action="review_quantity",
                        )
                    )
        margin = analyze_margin(row)
        if margin.get("margin_pct"):
            try:
                mp = float(margin["margin_pct"])
                if mp < -50 or mp > 95:
                    issues.append(
                        DataIssue(
                            row_ref=ref,
                            column="margin",
                            issue_type="unusual_margin",
                            severity=SEVERITY_WARNING,
                            description=f"Unusual margin_pct={mp}",
                            suggested_action="review_margin",
                        )
                    )
            except ValueError:
                pass
        if not any(row.get(k) for k in ("inn", "ean", "sku", "document_number")):
            if any(k in row for k in ("amount", "price", "company_name")):
                issues.append(
                    DataIssue(
                        row_ref=ref,
                        column="",
                        issue_type="missing_identifier",
                        severity=SEVERITY_WARNING,
                        description="Row lacks strong identifiers",
                        suggested_action="add_identifier",
                    )
                )
    return issues
