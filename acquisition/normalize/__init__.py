"""Versioned deterministic normalization — never invent currency/date/zero for missing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from acquisition.models import (
    EXTRACT_EMPTY,
    EXTRACT_INVALID,
    EXTRACT_MISSING,
    EXTRACT_OK,
    EXTRACT_UNAVAILABLE,
    NORMALIZER_VERSION,
    NormalizedRecord,
    ParsedRecord,
    fingerprint_record,
    new_id,
    utc_now,
)

# Ambiguous date patterns we refuse to invent a calendar date for.
_AMBIGUOUS_DATE = re.compile(
    r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$"
)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class NormalizeResult:
    record: NormalizedRecord
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


def _status_for(value) -> str:
    if value is None:
        return EXTRACT_MISSING
    if isinstance(value, str) and not value.strip():
        return EXTRACT_EMPTY
    return EXTRACT_OK


def normalize_currency(value, *, field_status: dict) -> str | None:
    """Return ISO currency code or None — never invent a default currency."""
    if value is None:
        field_status["currency"] = EXTRACT_MISSING
        return None
    text = str(value).strip().upper()
    if not text:
        field_status["currency"] = EXTRACT_EMPTY
        return None
    # Symbol-only without code → invalid (do not invent USD/EUR)
    if text in {"$", "€", "£", "¥", "₽"}:
        field_status["currency"] = EXTRACT_INVALID
        return None
    # Map common symbols only when paired was already a code; symbols alone stay invalid
    aliases = {"RUR": "RUB", "CNY": "CNY", "RMB": "CNY"}
    text = aliases.get(text, text)
    if _CURRENCY_CODE.match(text):
        field_status["currency"] = EXTRACT_OK
        return text
    field_status["currency"] = EXTRACT_INVALID
    return None


def normalize_date(value, *, field_status: dict, field: str = "date") -> str | None:
    """Return ISO date or None — refuse ambiguous DD/MM vs MM/DD without timezone context."""
    if value is None:
        field_status[field] = EXTRACT_MISSING
        return None
    if isinstance(value, datetime):
        field_status[field] = EXTRACT_OK
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        field_status[field] = EXTRACT_EMPTY
        return None
    m = _ISO_DATE.match(text)
    if m:
        field_status[field] = EXTRACT_OK
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    amb = _AMBIGUOUS_DATE.match(text)
    if amb:
        # Ambiguous numeric date — do not invent ordering
        field_status[field] = EXTRACT_INVALID
        return None
    field_status[field] = EXTRACT_UNAVAILABLE
    return None


def normalize_number(value, *, field_status: dict, field: str) -> float | None:
    """Parse number or None — never coerce missing to 0."""
    if value is None:
        field_status[field] = EXTRACT_MISSING
        return None
    if isinstance(value, bool):
        field_status[field] = EXTRACT_INVALID
        return None
    if isinstance(value, (int, float)):
        field_status[field] = EXTRACT_OK
        return float(value)
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text:
        field_status[field] = EXTRACT_EMPTY
        return None
    try:
        field_status[field] = EXTRACT_OK
        return float(text)
    except ValueError:
        field_status[field] = EXTRACT_INVALID
        return None


class RecordNormalizer:
    version: str = NORMALIZER_VERSION

    def normalize_parsed(
        self,
        record: ParsedRecord,
        *,
        job_id: str,
        resource_id: str = "",
    ) -> NormalizeResult:
        fields = dict(record.fields or {})
        out: dict = {}
        status: dict = {}
        warnings: list[str] = []
        errors: list[str] = []

        for key, value in fields.items():
            lk = str(key).lower()
            if lk in {"currency", "curr"}:
                cur = normalize_currency(value, field_status=status)
                if cur is not None:
                    out["currency"] = cur
                elif status.get("currency") == EXTRACT_INVALID:
                    warnings.append("currency_invalid")
                elif status.get("currency") == EXTRACT_MISSING:
                    warnings.append("currency_missing")
                continue
            if lk in {"date", "observed_date", "price_date", "as_of"}:
                dt = normalize_date(value, field_status=status, field=lk)
                if dt is not None:
                    out[lk] = dt
                elif status.get(lk) == EXTRACT_INVALID:
                    warnings.append(f"{lk}_ambiguous_or_invalid")
                continue
            if lk in {"price", "stock", "moq", "qty", "quantity", "amount"}:
                num = normalize_number(value, field_status=status, field=lk)
                if num is not None:
                    out[lk] = num
                elif status.get(lk) == EXTRACT_MISSING:
                    warnings.append(f"{lk}_missing")
                elif status.get(lk) == EXTRACT_INVALID:
                    errors.append(f"{lk}_invalid")
                continue
            # Pass-through identifiers / strings — mark status only
            st = _status_for(value)
            status[lk] = st
            if st == EXTRACT_OK:
                out[lk] = value
            elif st == EXTRACT_EMPTY:
                warnings.append(f"{lk}_empty")
            elif st == EXTRACT_MISSING:
                warnings.append(f"{lk}_missing")

        # Ensure we never invent price=0 / currency when absent
        if "price" not in out and "price" not in status:
            status["price"] = EXTRACT_MISSING
        if "currency" not in out and "currency" not in status:
            status["currency"] = EXTRACT_MISSING

        fp = fingerprint_record(
            {
                "source_id": record.source_id,
                "normalizer_version": self.version,
                **{k: out[k] for k in sorted(out)},
            }
        )
        normalized = NormalizedRecord(
            record_id=new_id("nrec-"),
            job_id=job_id,
            tenant_id=record.tenant_id,
            source_id=record.source_id,
            resource_id=resource_id or record.artifact_id,
            normalizer_version=self.version,
            fields=out,
            field_status=status,
            fingerprint=fp,
            warnings=tuple(warnings),
            errors=tuple(errors),
            provenance={
                "parsed_record_id": record.record_id,
                "parser_id": record.parser_id,
                "parser_version": record.parser_version,
                "artifact_id": record.artifact_id,
            },
            created_at=utc_now(),
        )
        return NormalizeResult(record=normalized, warnings=tuple(warnings), errors=tuple(errors))

    def normalize_fields(
        self,
        fields: Mapping[str, object],
        *,
        tenant_id: str,
        source_id: str,
        job_id: str,
        resource_id: str = "",
    ) -> NormalizeResult:
        # Synthetic ParsedRecord-like path
        from acquisition.models import RECORD_GENERIC, ParsedRecord, fingerprint_record as fp_fn

        synthetic = ParsedRecord(
            record_id=new_id("tmp-"),
            parser_id="direct",
            parser_version="0",
            source_id=source_id,
            artifact_id=resource_id or "",
            tenant_id=tenant_id,
            record_type=RECORD_GENERIC,
            fields=dict(fields),
            confidence=1.0,
            fingerprint=fp_fn(dict(fields)),
            observed_at=utc_now(),
        )
        return self.normalize_parsed(synthetic, job_id=job_id, resource_id=resource_id)
