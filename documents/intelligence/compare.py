"""Document comparison — structured + section-level."""

from __future__ import annotations

from documents.errors import COMPARISON_FAILED, DocumentError
from documents.intelligence.contracts import DocumentComparisonResult, StructuredDocument


def compare_structured(
    left: StructuredDocument,
    right: StructuredDocument,
) -> DocumentComparisonResult:
    try:
        changed = []
        left_fields = {**dict(left.fields), **dict(left.identifiers), **dict(left.amounts), **dict(left.dates)}
        right_fields = {**dict(right.fields), **dict(right.identifiers), **dict(right.amounts), **dict(right.dates)}
        keys = set(left_fields) | set(right_fields)
        for key in sorted(keys):
            if key == "classification_signals":
                continue
            lv = left_fields.get(key)
            rv = right_fields.get(key)
            if lv != rv:
                changed.append({"field": key, "old": lv, "new": rv})

        left_items = [dict(x) for x in left.line_items]
        right_items = [dict(x) for x in right.line_items]
        left_keys = {_item_key(i): i for i in left_items}
        right_keys = {_item_key(i): i for i in right_items}
        table_diff = []
        for k in sorted(set(left_keys) - set(right_keys)):
            table_diff.append({"op": "removed", "key": k, "row": left_keys[k]})
        for k in sorted(set(right_keys) - set(left_keys)):
            table_diff.append({"op": "added", "key": k, "row": right_keys[k]})
        for k in sorted(set(left_keys) & set(right_keys)):
            if left_keys[k] != right_keys[k]:
                table_diff.append(
                    {"op": "changed", "key": k, "old": left_keys[k], "new": right_keys[k]}
                )

        left_sections = {str(p.get("name") if isinstance(p, dict) else p) for p in left.parties}
        right_sections = {str(p.get("name") if isinstance(p, dict) else p) for p in right.parties}
        # also compare party roles as sections
        left_roles = {str(p.get("role")) for p in left.parties if isinstance(p, dict)}
        right_roles = {str(p.get("role")) for p in right.parties if isinstance(p, dict)}

        unchanged = not changed and not table_diff and left_roles == right_roles
        return DocumentComparisonResult(
            left_ref=left.document_id,
            right_ref=right.document_id,
            changed_fields=tuple(changed),
            added_sections=tuple(sorted(right_roles - left_roles)),
            removed_sections=tuple(sorted(left_roles - right_roles)),
            table_differences=tuple(table_diff),
            summary={
                "changed_field_count": len(changed),
                "table_diff_count": len(table_diff),
                "left_type": left.document_type,
                "right_type": right.document_type,
            },
            unchanged=unchanged,
        )
    except Exception as exc:
        raise DocumentError(COMPARISON_FAILED) from exc


def _item_key(item: dict) -> str:
    return str(
        item.get("sku")
        or item.get("ean")
        or item.get("name")
        or item.get("invoice_line")
        or hash(frozenset((k, str(v)) for k, v in sorted(item.items())))
    )


def compare_text_sections(left_text: str, right_text: str, *, left_ref: str, right_ref: str) -> DocumentComparisonResult:
    left_set = {l.strip() for l in left_text.splitlines() if l.strip()}
    right_set = {l.strip() for l in right_text.splitlines() if l.strip()}
    return DocumentComparisonResult(
        left_ref=left_ref,
        right_ref=right_ref,
        added_sections=tuple(sorted(right_set - left_set)[:200]),
        removed_sections=tuple(sorted(left_set - right_set)[:200]),
        summary={"mode": "text_sections"},
        unchanged=left_set == right_set,
    )
