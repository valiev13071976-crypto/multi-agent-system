"""Block 13 — SEO package from ProductCard. Reuses seo_marketing.metadata; no external keyword API."""

from __future__ import annotations

import re
import unicodedata

from seo_marketing.metadata import validate_meta

from product_content.category_policy import CategoryPolicy
from product_content.contracts import PROV_DERIVED, PROV_UNKNOWN, ProductCard, SeoPackage

_CYR_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

_FAKE_SEO = re.compile(
    r"(?i)(\b№\s*1\b|\bbestseller\b|\b5\s*stars?\b|\bofficial\s+dealer\b|"
    r"\bfree\s+delivery\s+tomorrow\b|\b50%\s*off\b|\bin\s+stock\s+now\b|"
    r"\brating\s*[:=]\s*\d|\breviewcount\s*[:=])"
)


def transliterate_local(text: str) -> str:
    nfkc = unicodedata.normalize("NFKC", text or "")
    out = []
    for ch in nfkc.casefold():
        if ch in _CYR_MAP:
            out.append(_CYR_MAP[ch])
        elif "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif ch in {" ", "-", "_", "/"}:
            out.append("-")
        else:
            out.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    return slug or "product"


def stable_slug(card: ProductCard, *, occupied: set[str] | None = None) -> tuple[str, bool]:
    occupied = occupied or set()
    base = transliterate_local(card.canonical_title or card.product_name or card.sku or card.product_id)
    if card.sku:
        candidate = f"{base}-{transliterate_local(card.sku)}"
    else:
        candidate = base
    collision = candidate in occupied and occupied  # occupied set used for this product family
    if candidate in occupied:
        candidate = f"{candidate}-{transliterate_local(card.product_id)[:12]}"
        collision = True
    return candidate, collision


def keyword_candidates(card: ProductCard) -> tuple[str, ...]:
    parts: list[str] = []
    for v in (card.brand, card.model, card.category, card.canonical_title, card.color, card.product_name):
        t = (v or "").strip()
        if t and t not in parts:
            parts.append(t)
    for pv in card.specifications.values():
        if pv.normalized and pv.provenance != PROV_UNKNOWN:
            token = f"{pv.role} {pv.normalized}".strip()
            if token not in parts:
                parts.append(token)
    return tuple(parts[:16])


def _schema_product(card: ProductCard, *, slug: str, image_refs: list[str]) -> dict:
    schema: dict = {
        "@type": "Product",
        "name": card.canonical_title or card.product_name,
        "sku": card.sku or None,
        "description": card.short_description or None,
        "url_slug": slug,
    }
    if card.brand:
        schema["brand"] = {"@type": "Brand", "name": card.brand}
    if image_refs:
        schema["image"] = list(image_refs)
    # Never invent rating, reviewCount, availability, GTIN, price, currency
    if card.barcode:
        schema["gtin"] = card.barcode
    schema = {k: v for k, v in schema.items() if v not in (None, "", [], {})}
    return schema


def build_seo_package(
    card: ProductCard,
    *,
    policy: CategoryPolicy,
    occupied_slugs: set[str] | None = None,
    occupied_titles: set[str] | None = None,
    image_refs: list[str] | None = None,
    extra_copy: str = "",
) -> SeoPackage:
    occupied_slugs = set(occupied_slugs or ())
    occupied_titles = set(occupied_titles or ())
    title = (card.canonical_title or card.product_name or card.sku)[: policy.seo_title_max]
    facts_bits = [card.brand, card.model, card.category, card.color]
    meta = ". ".join(p for p in [card.short_description, *[b for b in facts_bits if b]] if p)
    meta = meta[: policy.seo_meta_max]
    combined_scan = f"{title} {meta} {extra_copy}"
    claim_issues: list[str] = []
    if _FAKE_SEO.search(combined_scan):
        claim_issues.append("unsupported_seo_claim")
    validation = validate_meta(
        title=title,
        description=meta or " ",
        existing_titles=occupied_titles,
        trusted_facts={"category": card.category, "brand": card.brand},
        max_title=policy.seo_title_max,
        max_desc=policy.seo_meta_max,
    )
    slug, slug_collision = stable_slug(card, occupied=occupied_slugs)
    keywords = keyword_candidates(card)
    schema = _schema_product(card, slug=slug, image_refs=list(image_refs or ()))
    schema_ready = bool(schema.get("name") and schema.get("sku"))
    issues = list(validation.issues) + claim_issues
    warnings = list(validation.warnings)
    if slug_collision:
        issues.append("slug_collision")
    dup_title = "duplicate_title" in validation.issues
    quality = {
        "title_present": bool(title.strip()),
        "title_length": len(title),
        "title_length_ok": len(title) <= policy.seo_title_max,
        "meta_present": bool(meta.strip()),
        "meta_length": len(meta),
        "meta_length_ok": len(meta) <= policy.seo_meta_max,
        "slug_valid": bool(slug) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is not None,
        "duplicate_title": dup_title,
        "duplicate_slug": slug_collision,
        "missing_factual_fields": list(card.missing_required),
        "unsupported_claims": claim_issues,
        "keyword_stuffing": "keyword_stuffing" in validation.issues,
        "schema_readiness": schema_ready,
        "not_a_ranking_guarantee": True,
        "keywords_are_candidates_only": True,
        "no_search_volume": True,
        "no_cpc": True,
        "no_competition_metric": True,
    }
    sections = tuple(
        s for s in ("overview", "specifications", "package") if s != "specifications" or card.specifications
    )
    hints = tuple(h for h in (card.category, card.brand) if h)
    return SeoPackage(
        seo_title=title,
        meta_description=meta,
        canonical_slug=slug,
        heading=title,
        product_summary=card.short_description,
        keyword_candidates=keywords,
        keyword_note="CANDIDATES — not verified search-volume keywords; no CPC/rank/trend",
        structured_sections=sections,
        internal_link_hints=hints,
        schema_product=schema,
        schema_readiness=schema_ready,
        quality=quality,
        warnings=tuple(warnings),
        issues=tuple(issues),
        field_provenance={
            "seo_title": PROV_DERIVED,
            "meta_description": PROV_DERIVED,
            "canonical_slug": PROV_DERIVED,
            "keyword_candidates": PROV_DERIVED,
            "schema_product": PROV_DERIVED,
        },
        duplicate_title=dup_title,
        duplicate_slug=slug_collision,
    )
