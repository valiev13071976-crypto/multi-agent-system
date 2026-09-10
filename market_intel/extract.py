"""Deterministic offer extraction from a free-text supplier post.

Runs entirely on the message text: no web search, no LLM, no network.
Reuses the existing normalization primitives
(``product_intel.normalize`` -> ``acquisition.identifiers``) rather than
adding a second normalization engine.

The rule everywhere is report-what-was-read: an ambiguous signal yields
NOTHING for that field rather than a guess, because a wrong model or a
wrong currency silently corrupts a price comparison.
"""

from __future__ import annotations

import re

from product_intel.normalize import (
    normalize_brand,
    normalize_currency,
    normalize_ean,
    normalize_price,
    validate_ean,
)

from market_intel.models import ExtractedOffer

_LABEL_SPLIT = re.compile(r"^\s*([^:：]{2,30})\s*[:：]\s*(.+?)\s*$")
_WS = re.compile(r"\s+")

_PRICE_LABELS = frozenset(
    {"цена", "стоимость", "цена опт", "опт", "оптом", "оптовая цена", "price", "cost", "wholesale", "wholesale price"}
)
_EAN_LABELS = frozenset({"ean", "gtin", "штрихкод", "штрих-код", "шк", "barcode", "ean13", "ean-13"})
_MODEL_LABELS = frozenset(
    {"артикул", "арт", "art", "sku", "модель", "model", "mpn", "p/n", "pn", "партномер", "парт-номер", "код"}
)
_BRAND_LABELS = frozenset({"бренд", "brand", "производитель", "manufacturer", "марка", "вендор", "vendor"})

# A currency marker is what makes a bare number a PRICE. Without one, a
# number in a supplier post is just as likely to be a diagonal, a
# capacity or a quantity, so no price is reported at all.
_CURRENCY_MARKER = re.compile(
    r"(₽|руб\.?|р\.|rub|\$|usd|€|eur|₸|kzt|byn)",
    re.IGNORECASE,
)
_NUMBER = r"\d[\d\s\u00a0.,]*\d|\d"
_PRICE_BEFORE = re.compile(rf"({_NUMBER})\s*{_CURRENCY_MARKER.pattern}", re.IGNORECASE)
_PRICE_AFTER = re.compile(rf"{_CURRENCY_MARKER.pattern}\s*({_NUMBER})", re.IGNORECASE)

# Unit-suffixed numbers ("256GB", "55INCH", "120HZ") look exactly like a
# model code to a letters+digits rule, so they are excluded explicitly.
_UNIT_SUFFIXED = re.compile(
    r"^\d+(?:[.,]\d+)?"
    r"(?:GB|TB|MB|KB|ГБ|ТБ|МБ|MM|CM|ММ|СМ|W|ВТ|KW|КВТ|HZ|ГЦ|KHZ|MHZ|GHZ|INCH|IN|KG|КГ|ML|МЛ|MAH|МАЧ|K|К|P)$",
    re.IGNORECASE,
)
_TOKEN_SPLIT = re.compile(r"[\s,;()\[\]«»\"/]+")
_MODEL_MIN_LEN = 5


def _labelled_fields(text: str) -> dict[str, str]:
    """``label: value`` pairs, first occurrence of each label wins."""
    found: dict[str, str] = {}
    for line in str(text or "").splitlines():
        match = _LABEL_SPLIT.match(line)
        if not match:
            continue
        label = _WS.sub(" ", match.group(1)).strip().casefold().replace("ё", "е")
        value = match.group(2).strip()
        if label and value and label not in found:
            found[label] = value
    return found


def _first_labelled(fields: dict[str, str], labels: frozenset[str]) -> str:
    for label, value in fields.items():
        if label in labels:
            return value
    return ""


def _currency_of(token: str) -> str:
    """``руб.`` and ``р.`` are written with a trailing period as often as
    not; the shared alias table keys the bare forms."""
    raw = str(token or "").strip()
    return normalize_currency(raw) or normalize_currency(raw.rstrip("."))


def _price_and_currency(fragment: str) -> tuple[object, str]:
    """A number that is explicitly attached to a currency marker.

    The currency may still fail to resolve (an unusual symbol); the price
    is reported anyway with an empty currency, which downstream treats as
    not comparable rather than as a default.
    """
    for pattern, number_group, currency_group in ((_PRICE_BEFORE, 1, 2), (_PRICE_AFTER, 2, 1)):
        match = pattern.search(fragment or "")
        if match:
            price = normalize_price(match.group(number_group))
            if price is not None:
                return (price, _currency_of(match.group(currency_group)))
    return (None, "")


def _extract_price(text: str, fields: dict[str, str]) -> tuple[object, str]:
    labelled = _first_labelled(fields, _PRICE_LABELS)
    if labelled:
        price, currency = _price_and_currency(labelled)
        if price is not None:
            return (price, currency)
        # A labelled price without any currency marker is still a price;
        # the currency is simply unknown and is reported as such.
        bare = re.search(_NUMBER, labelled)
        if bare:
            return (normalize_price(bare.group(0)), "")
    for line in str(text or "").splitlines():
        if _CURRENCY_MARKER.search(line):
            price, currency = _price_and_currency(line)
            if price is not None:
                return (price, currency)
    return (None, "")


def _extract_ean(text: str, fields: dict[str, str]) -> str:
    labelled = _first_labelled(fields, _EAN_LABELS)
    if labelled:
        candidate = normalize_ean(labelled)
        if candidate and validate_ean(candidate):
            return candidate
    valid = {
        run
        for run in re.findall(r"\d{8,14}", str(text or ""))
        if len(run) in {8, 12, 13, 14} and validate_ean(run)
    }
    return valid.pop() if len(valid) == 1 else ""


def _looks_like_model(token: str) -> bool:
    if len(token) < _MODEL_MIN_LEN:
        return False
    if not (any(c.isalpha() for c in token) and any(c.isdigit() for c in token)):
        return False
    return not _UNIT_SUFFIXED.match(token)


def _extract_model(text: str, fields: dict[str, str], ean: str) -> str:
    labelled = _first_labelled(fields, _MODEL_LABELS)
    if labelled:
        token = _TOKEN_SPLIT.split(labelled.strip())[0].strip()
        if token:
            return token
    candidates: list[str] = []
    for raw in _TOKEN_SPLIT.split(str(text or "")):
        token = raw.strip().strip(".,:;")
        if not token or token == ean or token.isdigit():
            continue
        if _looks_like_model(token) and token not in candidates:
            candidates.append(token)
    # Two competing codes in one post (a bundle, or a model plus a promo
    # code) cannot be told apart deterministically -- report neither.
    return candidates[0] if len(candidates) == 1 else ""


def _extract_title(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = _WS.sub(" ", re.sub(r"^[^\w\d]+", "", line)).strip()
        if cleaned and not _LABEL_SPLIT.match(line):
            return cleaned
    return ""


def extract_offer(text: str) -> ExtractedOffer | None:
    """Normalize one supplier post, or ``None`` when it carries no offer.

    A post is an offer only when it has a price AND something to identify
    the product by; anything else is chatter and is not persisted as a
    market fact.
    """
    body = str(text or "")
    if not body.strip():
        return None
    fields = _labelled_fields(body)
    price, currency = _extract_price(body, fields)
    ean = _extract_ean(body, fields)
    model = _extract_model(body, fields, ean)
    offer = ExtractedOffer(
        raw_text=body,
        title=_extract_title(body),
        brand=normalize_brand(_first_labelled(fields, _BRAND_LABELS)),
        model=model,
        ean=ean,
        price=price,
        currency=currency,
    )
    if price is None or not offer.has_identifier():
        return None
    return offer
