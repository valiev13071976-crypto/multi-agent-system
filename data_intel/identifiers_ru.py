"""Russian company identifiers — INN / KPP / OGRN / OGRNIP (deterministic)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from data_intel.errors import INVALID_IDENTIFIER, DataIntelError

_DIGITS = re.compile(r"\D+")


@dataclass(frozen=True)
class IdentifierNormResult:
    normalized: str | None
    valid: bool
    kind: str
    error: str | None = None
    evidence: tuple[str, ...] = ()


def _digits_only(value: str | None) -> str:
    return _DIGITS.sub("", str(value or ""))


def _inn10_checksum(digits: str) -> bool:
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    total = sum(int(digits[i]) * weights[i] for i in range(9))
    return (total % 11) % 10 == int(digits[9])


def _inn12_checksum(digits: str) -> bool:
    w1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    w2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    c1 = (sum(int(digits[i]) * w1[i] for i in range(10)) % 11) % 10
    c2 = (sum(int(digits[i]) * w2[i] for i in range(11)) % 11) % 10
    return c1 == int(digits[10]) and c2 == int(digits[11])


def normalize_inn(value: str | None) -> IdentifierNormResult:
    digits = _digits_only(value)
    if not digits:
        return IdentifierNormResult(None, False, "inn", "empty")
    # Preserve leading zeros by keeping digit string (never float)
    if len(digits) == 10:
        ok = _inn10_checksum(digits)
        return IdentifierNormResult(
            digits,
            ok,
            "inn10",
            None if ok else "inn_checksum_invalid",
            ("length=10", "checksum_ok" if ok else "checksum_fail"),
        )
    if len(digits) == 12:
        ok = _inn12_checksum(digits)
        return IdentifierNormResult(
            digits,
            ok,
            "inn12",
            None if ok else "inn_checksum_invalid",
            ("length=12", "checksum_ok" if ok else "checksum_fail"),
        )
    return IdentifierNormResult(
        digits,
        False,
        "inn",
        "inn_length_invalid",
        (f"length={len(digits)}",),
    )


def normalize_kpp(value: str | None) -> IdentifierNormResult:
    digits = _digits_only(value)
    if not digits:
        return IdentifierNormResult(None, False, "kpp", "empty")
    if len(digits) != 9:
        return IdentifierNormResult(digits, False, "kpp", "kpp_length_invalid", (f"length={len(digits)}",))
    return IdentifierNormResult(digits, True, "kpp", None, ("length=9",))


def _ogrn_checksum(digits: str) -> bool:
    # OGRN 13: (n12 % 11) % 10 == check; OGRNIP 15: (n14 % 13) % 10 == check
    if len(digits) == 13:
        body = int(digits[:12])
        return (body % 11) % 10 == int(digits[12])
    if len(digits) == 15:
        body = int(digits[:14])
        return (body % 13) % 10 == int(digits[14])
    return False


def normalize_ogrn(value: str | None) -> IdentifierNormResult:
    digits = _digits_only(value)
    if not digits:
        return IdentifierNormResult(None, False, "ogrn", "empty")
    if len(digits) == 13:
        ok = _ogrn_checksum(digits)
        return IdentifierNormResult(
            digits, ok, "ogrn", None if ok else "ogrn_checksum_invalid", ("length=13",)
        )
    if len(digits) == 15:
        ok = _ogrn_checksum(digits)
        return IdentifierNormResult(
            digits, ok, "ogrnip", None if ok else "ogrn_checksum_invalid", ("length=15",)
        )
    return IdentifierNormResult(digits, False, "ogrn", "ogrn_length_invalid", (f"length={len(digits)}",))


def require_valid_inn(value: str | None) -> str:
    result = normalize_inn(value)
    if not result.valid or not result.normalized:
        raise DataIntelError(INVALID_IDENTIFIER)
    return result.normalized
