"""Fact-only, Russian-default generated content (requirement 4).

Deterministic template composition -- by construction, every factual
phrase is built directly from ``ResolvedIdentity``/``NormalizedCharacteristic``
values already present in the caller's enrichment data. Nothing here calls
an LLM or invents a number/spec that was not already verified upstream:
this keeps the "every factual claim must be supported by a verified fact"
contract trivially true and mechanically testable (see
``tests/test_product_enrichment_content.py``).
"""

from __future__ import annotations

from typing import Mapping

from product_enrichment.models import (
    ContentDraft,
    NormalizedCharacteristic,
    ResolvedIdentity,
)

# canonical_key -> Russian phrase template. ``{value}`` is substituted with
# the characteristic's own normalized value (+ unit where present) --
# never a free-form LLM-authored number.
_PHRASE_TEMPLATES: Mapping[str, str] = {
    "screen_diagonal_cm": "диагональ экрана {value} см",
    "screen_resolution": "разрешение экрана {value}",
    "panel_technology": "матрица {value}",
    "backlight_technology": "подсветка {value}",
    "refresh_rate_hz": "частота обновления {value} Гц",
    "hdr_formats": "поддержка HDR: {value}",
    "smart_tv_support": "Smart TV: {value}",
    "operating_system": "операционная система {value}",
    "color": "цвет {value}",
    "weight_with_stand_kg": "вес с подставкой {value} кг",
    "weight_without_stand_kg": "вес без подставки {value} кг",
}

_USABLE_CONFIDENCE = {"verified", "probable"}


def _usable_facts(characteristics: Mapping[str, NormalizedCharacteristic]) -> list[NormalizedCharacteristic]:
    return [c for c in characteristics.values() if c.confidence in _USABLE_CONFIDENCE]


def _phrase_for(characteristic: NormalizedCharacteristic) -> str | None:
    template = _PHRASE_TEMPLATES.get(characteristic.key)
    if template is None or not characteristic.value:
        return None
    return template.format(value=characteristic.value)


def generate_content(identity: ResolvedIdentity, characteristics: Mapping[str, NormalizedCharacteristic]) -> ContentDraft:
    """Builds short/detailed descriptions + SEO metadata strictly from
    ``identity`` and the subset of ``characteristics`` that are usable
    (verified/probable -- never unverified/conflicting) facts."""
    usable = _usable_facts(characteristics)
    phrases = [p for p in (_phrase_for(c) for c in usable) if p]
    facts_used = tuple(sorted(c.key for c in usable if _phrase_for(c)))

    name = f"{identity.brand} {identity.model}".strip()
    if phrases:
        short = f"{name} — с характеристиками: {', '.join(phrases[:3])}."
        detailed_lines = [f"{name}.", "Основные характеристики:"]
        detailed_lines.extend(f"- {p}." for p in phrases)
        detailed = "\n".join(detailed_lines)
    else:
        short = f"{name}."
        detailed = f"{name}."

    seo_title = f"{name} — купить"
    seo_description = short[:160]
    keywords = [identity.brand, identity.model]
    if identity.category:
        keywords.append(identity.category)
    if identity.subcategory:
        keywords.append(identity.subcategory)
    image_alt = name
    image_title = name

    return ContentDraft(
        short_description=short,
        detailed_description=detailed,
        seo_title=seo_title,
        seo_description=seo_description,
        seo_keywords=tuple(dict.fromkeys(k for k in keywords if k)),
        image_alt=image_alt,
        image_title=image_title,
        facts_used=facts_used,
    )
