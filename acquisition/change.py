"""Change detection for artifacts and structured records."""

from __future__ import annotations

from acquisition.models import (
    CHANGE_CHANGED,
    CHANGE_CREATED,
    CHANGE_REMOVED,
    CHANGE_UNCHANGED,
    ChangeEvent,
    ParsedRecord,
    new_id,
    utc_now,
)


_WATCH_FIELDS = ("price", "stock", "currency", "availability", "moq", "name", "sku", "ean")


def detect_record_change(
    *,
    previous: ParsedRecord | None,
    current: ParsedRecord,
) -> ChangeEvent:
    if previous is None:
        return ChangeEvent(
            change_id=new_id("chg-"),
            tenant_id=current.tenant_id,
            source_id=current.source_id,
            record_id=current.record_id,
            outcome=CHANGE_CREATED,
            previous_fingerprint=None,
            new_fingerprint=current.fingerprint,
            observed_at=utc_now(),
        )
    if previous.fingerprint == current.fingerprint:
        return ChangeEvent(
            change_id=new_id("chg-"),
            tenant_id=current.tenant_id,
            source_id=current.source_id,
            record_id=current.record_id,
            outcome=CHANGE_UNCHANGED,
            previous_fingerprint=previous.fingerprint,
            new_fingerprint=current.fingerprint,
            observed_at=utc_now(),
        )
    changed = []
    pf = dict(previous.fields)
    cf = dict(current.fields)
    for key in _WATCH_FIELDS:
        if pf.get(key) != cf.get(key) and (key in pf or key in cf):
            changed.append(key)
    return ChangeEvent(
        change_id=new_id("chg-"),
        tenant_id=current.tenant_id,
        source_id=current.source_id,
        record_id=current.record_id,
        outcome=CHANGE_CHANGED,
        previous_fingerprint=previous.fingerprint,
        new_fingerprint=current.fingerprint,
        changed_fields=tuple(changed),
        observed_at=utc_now(),
        metadata={"watch_fields": list(changed)},
    )


def detect_removal(
    *,
    previous: ParsedRecord,
    source_id: str,
) -> ChangeEvent:
    return ChangeEvent(
        change_id=new_id("chg-"),
        tenant_id=previous.tenant_id,
        source_id=source_id,
        record_id=previous.record_id,
        outcome=CHANGE_REMOVED,
        previous_fingerprint=previous.fingerprint,
        new_fingerprint=None,
        observed_at=utc_now(),
    )


def artifact_unchanged(previous_checksum: str | None, current_checksum: str) -> bool:
    return bool(previous_checksum) and previous_checksum == current_checksum
