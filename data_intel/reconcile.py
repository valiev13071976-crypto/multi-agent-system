"""Payment / transaction and VAT / amount reconciliation foundations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from data_intel.cleaning import clean_text, normalize_date, normalize_decimal_string
from data_intel.counterparty import match_counterparties, normalize_legal_name
from data_intel.identifiers_ru import normalize_inn


def _dec(value) -> Decimal | None:
    text = normalize_decimal_string(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_date(value) -> datetime | None:
    iso = normalize_date(value)
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def reconcile_payments(
    bank_rows: list[dict],
    ledger_rows: list[dict],
    *,
    amount_tolerance: Decimal = Decimal("0.01"),
    date_window_days: int = 3,
) -> dict:
    """Match bank/payment table to accounting/report rows. No posting."""
    used_ledger: set[int] = set()
    matched, partial, unmatched_bank, unmatched_ledger = [], [], [], []
    duplicates, amount_mismatch, date_mismatch = [], [], []

    # Detect duplicate bank payments
    seen = {}
    for i, row in enumerate(bank_rows):
        key = (
            normalize_inn(row.get("inn")).normalized or "",
            str(_dec(row.get("amount")) or ""),
            normalize_date(row.get("payment_date") or row.get("date")) or "",
            clean_text(row.get("document_number")) or "",
        )
        if key in seen and any(key):
            duplicates.append({"indices": [seen[key], i], "key": list(key)})
        else:
            seen[key] = i

    for bi, brow in enumerate(bank_rows):
        b_amt = _dec(brow.get("amount"))
        b_date = _parse_date(brow.get("payment_date") or brow.get("date"))
        b_inn = normalize_inn(brow.get("inn"))
        b_doc = clean_text(brow.get("document_number") or brow.get("invoice_number"))
        b_purpose = clean_text(brow.get("purpose") or brow.get("payment_purpose"))
        best = None
        best_score = -1
        for li, lrow in enumerate(ledger_rows):
            if li in used_ledger:
                continue
            score = 0
            evidence = {}
            l_inn = normalize_inn(lrow.get("inn"))
            if b_inn.valid and l_inn.valid:
                if b_inn.normalized == l_inn.normalized:
                    score += 50
                    evidence["inn"] = b_inn.normalized
                else:
                    continue
            l_amt = _dec(lrow.get("amount"))
            if b_amt is not None and l_amt is not None:
                diff = abs(b_amt - l_amt)
                if diff <= amount_tolerance:
                    score += 40
                    evidence["amount"] = str(b_amt)
                elif diff <= amount_tolerance * 10:
                    score += 10
                    evidence["amount_near"] = str(diff)
                else:
                    continue
            l_date = _parse_date(lrow.get("payment_date") or lrow.get("date"))
            if b_date and l_date:
                delta = abs((b_date - l_date).days)
                if delta <= date_window_days:
                    score += 20
                    evidence["date_delta"] = delta
                else:
                    evidence["date_delta"] = delta
            l_doc = clean_text(lrow.get("document_number") or lrow.get("invoice_number"))
            if b_doc and l_doc and b_doc == l_doc:
                score += 25
                evidence["document"] = b_doc
            cp = match_counterparties(brow, lrow, left_ref=f"b{bi}", right_ref=f"l{li}")
            if cp.same_entity:
                score += 15
                evidence["counterparty"] = cp.match_method
            if b_purpose and clean_text(lrow.get("purpose")):
                if normalize_legal_name(b_purpose) == normalize_legal_name(lrow.get("purpose")):
                    score += 5
            if score > best_score:
                best_score = score
                best = (li, lrow, evidence, score)

        if best is None or best_score < 40:
            unmatched_bank.append({"index": bi, "row": brow})
            continue
        li, lrow, evidence, score = best
        used_ledger.add(li)
        item = {"bank_index": bi, "ledger_index": li, "score": score, "evidence": evidence, "bank": brow, "ledger": lrow}
        b_amt = _dec(brow.get("amount"))
        l_amt = _dec(lrow.get("amount"))
        if b_amt is not None and l_amt is not None and abs(b_amt - l_amt) > amount_tolerance:
            amount_mismatch.append(item)
            partial.append(item)
        elif evidence.get("date_delta", 0) > date_window_days:
            date_mismatch.append(item)
            partial.append(item)
        elif score >= 70:
            matched.append(item)
        else:
            partial.append(item)

    for li, lrow in enumerate(ledger_rows):
        if li not in used_ledger:
            unmatched_ledger.append({"index": li, "row": lrow})

    return {
        "matched": matched,
        "partially_matched": partial,
        "unmatched_bank": unmatched_bank,
        "unmatched_ledger": unmatched_ledger,
        "duplicate_payment": duplicates,
        "amount_mismatch": amount_mismatch,
        "date_mismatch": date_mismatch,
    }


def reconcile_vat_amounts(
    rows: list[dict],
    *,
    tolerance: Decimal = Decimal("0.05"),
    default_vat_rate: Decimal | None = None,
) -> dict:
    """Validate subtotal / VAT / total consistency."""
    issues = []
    for i, row in enumerate(rows):
        subtotal = _dec(row.get("subtotal") or row.get("amount_ex_vat"))
        vat = _dec(row.get("vat_amount") or row.get("vat"))
        total = _dec(row.get("total") or row.get("amount"))
        rate = _dec(row.get("vat_rate")) or default_vat_rate
        if subtotal is not None and vat is not None and total is not None:
            expected = subtotal + vat
            if abs(expected - total) > tolerance:
                issues.append(
                    {
                        "index": i,
                        "issue_type": "totals_mismatch",
                        "severity": "error",
                        "expected_total": str(expected),
                        "actual_total": str(total),
                    }
                )
            elif abs(expected - total) > 0:
                issues.append(
                    {
                        "index": i,
                        "issue_type": "rounding_difference",
                        "severity": "warning",
                        "diff": str(expected - total),
                    }
                )
        if subtotal is not None and rate is not None and vat is not None:
            expected_vat = (subtotal * rate / Decimal("100")).quantize(Decimal("0.01"))
            if abs(expected_vat - vat) > tolerance:
                issues.append(
                    {
                        "index": i,
                        "issue_type": "invalid_vat",
                        "severity": "error",
                        "expected_vat": str(expected_vat),
                        "actual_vat": str(vat),
                    }
                )
        # line sum vs document sum if present
        line_sum = _dec(row.get("line_sum"))
        doc_sum = _dec(row.get("document_sum") or total)
        if line_sum is not None and doc_sum is not None and abs(line_sum - doc_sum) > tolerance:
            issues.append(
                {
                    "index": i,
                    "issue_type": "line_document_sum_mismatch",
                    "severity": "error",
                    "line_sum": str(line_sum),
                    "document_sum": str(doc_sum),
                }
            )
    return {"issues": issues, "ok": not any(i["severity"] == "error" for i in issues)}
