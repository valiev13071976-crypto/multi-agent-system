"""Counterparty matching — INN-first, conflicts block fuzzy merge."""

from __future__ import annotations

import re

from data_intel.cleaning import clean_text
from data_intel.contracts import (
    CONF_CONFLICT,
    CONF_EXACT,
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    CONF_UNRESOLVED,
    MatchResult,
)
from data_intel.identifiers_ru import normalize_inn, normalize_kpp, normalize_ogrn

_LEGAL = re.compile(
    r"\b(ооо|оао|зао|пао|ао|ип|llc|ltd|inc|gmbh|company|компания)\b",
    re.I,
)
_WS = re.compile(r"\s+")


def normalize_legal_name(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.lower().replace("ё", "е")
    text = text.replace('"', "").replace("«", "").replace("»", "").replace("'", "")
    text = _LEGAL.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text or None


def _name_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_counterparties(
    left: dict,
    right: dict,
    *,
    left_ref: str = "left",
    right_ref: str = "right",
) -> MatchResult:
    """Match two counterparty field maps. Priority: INN → INN+KPP → OGRN → name."""
    l_inn = normalize_inn(left.get("inn"))
    r_inn = normalize_inn(right.get("inn"))
    l_kpp = normalize_kpp(left.get("kpp"))
    r_kpp = normalize_kpp(right.get("kpp"))
    l_ogrn = normalize_ogrn(left.get("ogrn"))
    r_ogrn = normalize_ogrn(right.get("ogrn"))
    l_name = normalize_legal_name(
        left.get("company_name") or left.get("counterparty") or left.get("name")
    )
    r_name = normalize_legal_name(
        right.get("company_name") or right.get("counterparty") or right.get("name")
    )

    if l_inn.valid and r_inn.valid and l_inn.normalized != r_inn.normalized:
        return MatchResult(
            entity_type="counterparty",
            left_ref=left_ref,
            right_ref=right_ref,
            match_method="inn_conflict",
            confidence=CONF_CONFLICT,
            evidence={"left_inn": l_inn.normalized, "right_inn": r_inn.normalized},
            conflicts=("inn_mismatch",),
            same_entity=False,
            review_required=True,
        )

    if l_inn.valid and r_inn.valid and l_inn.normalized == r_inn.normalized:
        if l_kpp.valid and r_kpp.valid and l_kpp.normalized == r_kpp.normalized:
            return MatchResult(
                entity_type="counterparty",
                left_ref=left_ref,
                right_ref=right_ref,
                match_method="inn_kpp_exact",
                confidence=CONF_EXACT,
                evidence={"inn": l_inn.normalized, "kpp": l_kpp.normalized},
                same_entity=True,
            )
        return MatchResult(
            entity_type="counterparty",
            left_ref=left_ref,
            right_ref=right_ref,
            match_method="inn_exact",
            confidence=CONF_EXACT,
            evidence={"inn": l_inn.normalized},
            same_entity=True,
        )

    if l_ogrn.valid and r_ogrn.valid and l_ogrn.normalized == r_ogrn.normalized:
        return MatchResult(
            entity_type="counterparty",
            left_ref=left_ref,
            right_ref=right_ref,
            match_method="ogrn_exact",
            confidence=CONF_EXACT,
            evidence={"ogrn": l_ogrn.normalized},
            same_entity=True,
        )

    if l_name and r_name:
        sim = _name_similarity(l_name, r_name)
        if sim >= 0.99:
            return MatchResult(
                entity_type="counterparty",
                left_ref=left_ref,
                right_ref=right_ref,
                match_method="normalized_legal_name",
                confidence=CONF_HIGH,
                evidence={"name": l_name, "similarity": sim},
                same_entity=True,
            )
        if sim >= 0.7:
            return MatchResult(
                entity_type="counterparty",
                left_ref=left_ref,
                right_ref=right_ref,
                match_method="fuzzy_legal_name",
                confidence=CONF_MEDIUM if sim >= 0.85 else CONF_LOW,
                evidence={"left_name": l_name, "right_name": r_name, "similarity": sim},
                same_entity=False,
                review_required=True,
            )

    return MatchResult(
        entity_type="counterparty",
        left_ref=left_ref,
        right_ref=right_ref,
        match_method="unresolved",
        confidence=CONF_UNRESOLVED,
        evidence={},
        same_entity=False,
        review_required=True,
    )
