"""Generic multi-document reconciliation — Decimal-exact numerics, no SKU fuzzy match."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping

from documents.intelligence.contracts import StructuredDocument
from documents.platform_models import (
    RECON_AMBIGUOUS,
    RECON_INSUFFICIENT_DATA,
    RECON_MATCH,
    RECON_MISMATCH,
    RECON_PARTIAL,
    ReconciliationIssue,
    ReconciliationProfile,
    ReconciliationResult,
)


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Reject binary float for monetary equality.
        return None
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _field_bag(doc: StructuredDocument) -> dict:
    bag = {}
    bag.update(dict(doc.fields))
    bag.update(dict(doc.identifiers))
    bag.update(dict(doc.amounts))
    bag.update(dict(doc.dates))
    return bag


def reconcile_documents(
    role_docs: Mapping[str, StructuredDocument],
    profile: ReconciliationProfile | None = None,
) -> ReconciliationResult:
    """Reconcile a multi-document role set against a versioned profile.

    ``role_docs`` maps role name → StructuredDocument (invoice/act/contract/...).
    """
    profile = profile or ReconciliationProfile(profile_id="default")
    roles = {str(k): v for k, v in dict(role_docs or {}).items()}
    issues: list[ReconciliationIssue] = []
    matched: list[str] = []

    if profile.require_all_roles and profile.role_pairs:
        needed = set()
        for a, b in profile.role_pairs:
            needed.add(a)
            needed.add(b)
        for role in sorted(needed):
            if role not in roles:
                issues.append(
                    ReconciliationIssue(
                        code="missing_role",
                        severity="error",
                        field=f"role:{role}",
                        left_role=role,
                        message="required_role_missing",
                    )
                )
        if any(i.code == "missing_role" for i in issues):
            return ReconciliationResult(
                status=RECON_INSUFFICIENT_DATA,
                issues=tuple(issues),
                profile_id=profile.profile_id,
                profile_version=profile.version,
                roles=tuple(sorted(roles)),
                evidence=("require_all_roles",),
            )

    if len(roles) < 2:
        return ReconciliationResult(
            status=RECON_INSUFFICIENT_DATA,
            issues=(
                ReconciliationIssue(
                    code="insufficient_roles",
                    severity="error",
                    message="need_at_least_two_documents",
                ),
            ),
            profile_id=profile.profile_id,
            profile_version=profile.version,
            roles=tuple(sorted(roles)),
        )

    pairs = list(profile.role_pairs) or []
    if not pairs:
        keys = sorted(roles)
        pairs = [(keys[0], keys[1])]

    bags = {role: _field_bag(doc) for role, doc in roles.items()}

    for left_role, right_role in pairs:
        if left_role not in bags or right_role not in bags:
            issues.append(
                ReconciliationIssue(
                    code="missing_role",
                    severity="error",
                    left_role=left_role,
                    right_role=right_role,
                    message="role_pair_incomplete",
                )
            )
            continue
        left = bags[left_role]
        right = bags[right_role]

        for field in profile.monetary_fields:
            lv = left.get(field)
            rv = right.get(field)
            if lv is None and rv is None:
                continue
            if lv is None or rv is None:
                issues.append(
                    ReconciliationIssue(
                        code="missing_field",
                        severity="warning",
                        field=field,
                        left_role=left_role,
                        right_role=right_role,
                        left_value=lv,
                        right_value=rv,
                        message="monetary_field_missing",
                    )
                )
                continue
            ld = _as_decimal(lv)
            rd = _as_decimal(rv)
            if ld is None or rd is None:
                issues.append(
                    ReconciliationIssue(
                        code="ambiguous_numeric",
                        severity="warning",
                        field=field,
                        left_role=left_role,
                        right_role=right_role,
                        left_value=lv,
                        right_value=rv,
                        message="non_decimal_or_float_rejected",
                    )
                )
                continue
            tol = profile.monetary_tolerance
            if abs(ld - rd) > tol:
                issues.append(
                    ReconciliationIssue(
                        code="monetary_mismatch",
                        severity="error",
                        field=field,
                        left_role=left_role,
                        right_role=right_role,
                        left_value=str(ld),
                        right_value=str(rd),
                        message=f"delta={abs(ld - rd)} tol={tol}",
                    )
                )
            else:
                matched.append(f"{left_role}.{field}={right_role}.{field}")

        for field in profile.date_fields:
            lv = left.get(field)
            rv = right.get(field)
            if lv is None and rv is None:
                continue
            if lv is None or rv is None:
                issues.append(
                    ReconciliationIssue(
                        code="missing_field",
                        severity="warning",
                        field=field,
                        left_role=left_role,
                        right_role=right_role,
                        left_value=lv,
                        right_value=rv,
                        message="date_field_missing",
                    )
                )
                continue
            ld = _as_date(lv)
            rd = _as_date(rv)
            if ld is None or rd is None:
                issues.append(
                    ReconciliationIssue(
                        code="ambiguous_date",
                        severity="warning",
                        field=field,
                        left_role=left_role,
                        right_role=right_role,
                        left_value=lv,
                        right_value=rv,
                        message="ambiguous_or_unnormalized_date",
                    )
                )
                continue
            if ld != rd:
                issues.append(
                    ReconciliationIssue(
                        code="date_mismatch",
                        severity="error",
                        field=field,
                        left_role=left_role,
                        right_role=right_role,
                        left_value=ld.isoformat(),
                        right_value=rd.isoformat(),
                        message="date_mismatch",
                    )
                )
            else:
                matched.append(f"{left_role}.{field}={right_role}.{field}")

        for field in profile.identifier_fields:
            lv = left.get(field)
            rv = right.get(field)
            if lv is None and rv is None:
                continue
            if lv is None or rv is None or str(lv).strip() != str(rv).strip():
                issues.append(
                    ReconciliationIssue(
                        code="identifier_mismatch",
                        severity="error",
                        field=field,
                        left_role=left_role,
                        right_role=right_role,
                        left_value=lv,
                        right_value=rv,
                        message="identifier_mismatch",
                    )
                )
            else:
                matched.append(f"{left_role}.{field}={right_role}.{field}")

    codes = {i.code for i in issues}
    if not issues:
        status = RECON_MATCH
    elif "ambiguous_date" in codes or "ambiguous_numeric" in codes:
        if any(c.endswith("mismatch") for c in codes):
            status = RECON_PARTIAL
        else:
            status = RECON_AMBIGUOUS
    elif any(c.endswith("mismatch") for c in codes):
        status = RECON_MISMATCH
    elif "missing_field" in codes or "missing_role" in codes:
        status = RECON_PARTIAL if matched else RECON_INSUFFICIENT_DATA
    else:
        status = RECON_PARTIAL

    return ReconciliationResult(
        status=status,
        issues=tuple(issues),
        matched_fields=tuple(matched),
        profile_id=profile.profile_id,
        profile_version=profile.version,
        roles=tuple(sorted(roles)),
        evidence=("engine:documents.reconcile",),
    )
