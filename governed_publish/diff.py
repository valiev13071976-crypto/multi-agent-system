"""Deterministic desired vs snapshot diff. Missing source does not delete target."""

from __future__ import annotations

from governed_publish.contracts import (
    DIFF_BLOCKED,
    DIFF_CREATE,
    DIFF_UNCHANGED,
    DIFF_UPDATE,
    DiffEntry,
)


PRESERVE_IF_ABSENT = frozenset(
    {
        "warranty",
        "country_of_origin",
        "manufacturer",
        "weight",
        "dimensions",
        "listing_price_preview_only",
        "stock",
        "discount",
    }
)


def classify_diff(*, desired: dict, snapshot: dict | None, remove_fields: frozenset[str] | None = None) -> list[DiffEntry]:
    remove_fields = remove_fields or frozenset()
    current = dict(snapshot or {})
    entries: list[DiffEntry] = []
    if snapshot is None:
        for k, v in desired.items():
            entries.append(DiffEntry(field=k, classification=DIFF_CREATE, desired=v, current=None))
        return entries
    keys = sorted(set(desired) | set(current))
    for k in keys:
        if k in desired:
            if k not in current:
                entries.append(DiffEntry(field=k, classification=DIFF_CREATE, desired=desired[k], current=None))
            elif desired[k] == current[k]:
                entries.append(DiffEntry(field=k, classification=DIFF_UNCHANGED, desired=desired[k], current=current[k]))
            else:
                entries.append(DiffEntry(field=k, classification=DIFF_UPDATE, desired=desired[k], current=current[k]))
        else:
            if k in PRESERVE_IF_ABSENT and k not in remove_fields:
                entries.append(
                    DiffEntry(
                        field=k,
                        classification=DIFF_UNCHANGED,
                        desired=current[k],
                        current=current[k],
                        omitted=True,
                    )
                )
            elif k in remove_fields:
                entries.append(DiffEntry(field=k, classification="REMOVE_REQUESTED", desired=None, current=current[k]))
            else:
                entries.append(
                    DiffEntry(
                        field=k,
                        classification=DIFF_UNCHANGED,
                        desired=current[k],
                        current=current[k],
                        omitted=True,
                    )
                )
    return entries


def summarize(entries: list[DiffEntry]) -> dict:
    create = tuple(e.field for e in entries if e.classification == DIFF_CREATE)
    change = tuple(e.field for e in entries if e.classification == DIFF_UPDATE)
    unchanged = tuple(e.field for e in entries if e.classification == DIFF_UNCHANGED)
    omitted = tuple(e.field for e in entries if e.omitted)
    blocked = tuple(e.field for e in entries if e.classification == DIFF_BLOCKED)
    if not entries:
        action = DIFF_UNCHANGED
    elif all(e.classification == DIFF_CREATE for e in entries if not e.omitted):
        action = DIFF_CREATE
    elif change:
        action = DIFF_UPDATE
    else:
        action = DIFF_UNCHANGED
    return {
        "action": action,
        "fields_create": create,
        "fields_change": change,
        "fields_unchanged": unchanged,
        "fields_omitted": omitted,
        "blocked_fields": blocked,
    }
