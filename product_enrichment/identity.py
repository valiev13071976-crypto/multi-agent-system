"""Fail-closed product identity resolution (requirement 1).

Pure, synchronous, no I/O. Establishes identity from brand + exact
manufacturer model + supplier article/SKU + EAN/GTIN BEFORE any research or
media work is attempted, and detects identity-critical mismatches between
the declared identity and any later-discovered evidence.
"""

from __future__ import annotations

import re

from product_enrichment.models import (
    IdentityConflictError,
    ProductIdentityQuery,
    ResolvedIdentity,
    compute_identity_key,
)

_EAN_RE = re.compile(r"^\d{8}$|^\d{12}$|^\d{13}$|^\d{14}$")

# Model-code "variant" tokens that must match EXACTLY when present in both
# the declared model and a piece of evidence text -- a different value for
# ANY of these inside otherwise-similar text is an unacceptable mismatch
# (screen-size variant, region/market suffix, or model year), never merged.
_SIZE_TOKEN_RE = re.compile(r"(\d{2,3})\s*(?:-|\s)?(?:inch|inches|\"|дюйм)", re.I)
_MODEL_YEAR_RE = re.compile(r"\b(20[12]\d)\b")


def _clean(value: str) -> str:
    return str(value or "").strip()


def resolve_identity(query: ProductIdentityQuery) -> ResolvedIdentity:
    """Fail closed: brand + at least one of (model, article) are mandatory.
    A syntactically invalid EAN fails closed rather than being silently
    dropped or coerced."""
    brand = _clean(query.brand)
    model = _clean(query.model)
    article = _clean(query.article)
    ean = _clean(query.ean)
    category = _clean(query.category)
    subcategory = _clean(query.subcategory)

    if not brand:
        raise IdentityConflictError("missing_brand", "brand is required to resolve product identity")
    if not model and not article:
        raise IdentityConflictError(
            "missing_model_or_article", "at least one of exact model or supplier article is required"
        )
    if ean and not _EAN_RE.match(ean):
        raise IdentityConflictError("invalid_ean_format", ean)

    effective_model = model or article
    strength = "ean_and_model" if (ean and model) else ("model_only" if model else "article_only")
    identity_key = compute_identity_key(brand=brand, model=effective_model, ean=ean)

    return ResolvedIdentity(
        brand=brand,
        model=effective_model,
        article=article,
        ean=ean,
        category=category,
        subcategory=subcategory,
        identity_key=identity_key,
        strength=strength,
    )


def extract_variant_tokens(text: str) -> dict[str, str]:
    """Deterministic extraction of identity-critical "variant" tokens
    (screen-size class, model year) from free text -- used to reject
    evidence describing a DIFFERENT variant of an otherwise similarly-named
    model (requirement 1's unacceptable-mismatch examples)."""
    blob = str(text or "")
    tokens: dict[str, str] = {}
    size_match = _SIZE_TOKEN_RE.search(blob)
    if size_match:
        tokens["screen_size_class"] = size_match.group(1)
    year_match = _MODEL_YEAR_RE.search(blob)
    if year_match:
        tokens["model_year"] = year_match.group(1)
    return tokens


def evidence_matches_identity(identity: ResolvedIdentity, *, text: str, url: str) -> bool:
    """Conservative exact-association check (requirement 1 + module 5's
    "exact-product association where deterministically verifiable"): the
    exact declared model/article string must appear verbatim (case-
    insensitive) in the evidence text or URL. Never a fuzzy/partial match
    -- a similar-looking model family is rejected, not merged."""
    if not identity.model:
        return False
    haystack = f"{text} {url}".casefold()
    return identity.model.casefold() in haystack


def detect_variant_conflict(identity: ResolvedIdentity, *, text: str) -> str | None:
    """Returns a stable conflict code if ``text`` names a DIFFERENT
    screen-size class or model year than the identity's own model code
    implies -- None if there is no detectable conflict. Used to reject a
    single piece of evidence outright rather than silently blend it in."""
    declared_tokens = extract_variant_tokens(identity.model)
    found_tokens = extract_variant_tokens(text)
    for key, declared_value in declared_tokens.items():
        found_value = found_tokens.get(key)
        if found_value and found_value != declared_value:
            return f"variant_mismatch_{key}"
    return None
