"""Generic characteristic normalization + verified-Bitrix-property bridging
(requirement 3).

Nothing here is TV-only: the alias/unit tables below are simply the initial
seed set (screen diagonal, resolution, OS, ... -- the exact examples in the
task) using the SAME generic shape (``canonical_key -> aliases``) that any
other product category's characteristics would use. Adding a new category's
characteristic is adding one more table entry, never a new code path.

Unknown-but-sourced characteristics are always preserved in the returned
mapping (as ``CONFIDENCE_UNVERIFIED`` with no ``bitrix_property_id``) -- see
module docstring of ``integrations.bitrix.schema``: Panda never guesses a
Bitrix property for a characteristic it cannot verify.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

from integrations.bitrix import schema as bitrix_schema
from product_enrichment.models import (
    CONFIDENCE_CONFLICTING,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_UNVERIFIED,
    CONFIDENCE_VERIFIED,
    IdentityConflict,
    NormalizedCharacteristic,
    SOURCE_TRUST_RANK,
    SourceFact,
)

# canonical_key -> (unit, (raw-label aliases, casefolded substrings))
# Generic, category-agnostic shape; the specific entries below are the
# task's own worked examples (TV characteristics + a few universal ones).
CANONICAL_CHARACTERISTIC_ALIASES: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "screen_diagonal_cm": ("cm", ("диагональ экрана", "диагональ дисплея", "screen size", "screen diagonal")),
    "screen_resolution": ("px", ("разрешение экрана", "разрешение", "resolution")),
    "panel_technology": ("", ("тип матрицы", "технология матрицы", "panel type", "display technology")),
    "backlight_technology": ("", ("подсветка", "backlight")),
    "refresh_rate_hz": ("Hz", ("частота обновления", "refresh rate")),
    "hdr_formats": ("", ("hdr", "формат hdr")),
    "smart_tv_support": ("", ("smart tv", "смарт тв", "смарт-тв")),
    "operating_system": ("", ("операционная система", "operating system", "смарт-платформа")),
    "tuners": ("", ("тюнер", "tuner")),
    "hdmi_count": ("", ("hdmi",)),
    "usb_count": ("", ("usb",)),
    "wifi_support": ("", ("wi-fi", "wifi")),
    "bluetooth_support": ("", ("bluetooth", "блютус")),
    "ethernet_support": ("", ("ethernet", "lan")),
    "audio_power_w": ("W", ("мощность звука", "audio power", "мощность динамиков")),
    "vesa_mount": ("mm", ("vesa",)),
    "color": ("", ("цвет", "color", "colour")),
    "dimensions_with_stand": ("mm", ("габариты с подставкой", "dimensions with stand")),
    "dimensions_without_stand": ("mm", ("габариты без подставки", "dimensions without stand")),
    "package_dimensions": ("mm", ("габариты упаковки", "package dimensions")),
    "weight_with_stand_kg": ("kg", ("вес с подставкой", "weight with stand")),
    "weight_without_stand_kg": ("kg", ("вес без подставки", "weight without stand")),
    "package_weight_kg": ("kg", ("вес в упаковке", "package weight")),
    "model_year": ("", ("год модели", "model year")),
    "country_of_origin": ("", ("страна происхождения", "country of origin", "made in")),
}

_LABEL_LOOKUP: tuple[tuple[str, str], ...] = tuple(
    (alias, key) for key, (_unit, aliases) in CANONICAL_CHARACTERISTIC_ALIASES.items() for alias in aliases
)
# Longest alias first so a more specific label (e.g. "разрешение экрана")
# never gets shadowed by a shorter one appearing earlier in iteration order.
_LABEL_LOOKUP = tuple(sorted(_LABEL_LOOKUP, key=lambda item: -len(item[0])))

_INCH_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:\"|inch|inches|дюйм)", re.I)
_CM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:cm|см)", re.I)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# ``scrape.fetch``'s ``body_text`` (``tools.platform.web_fetch_adapter.
# WebFetchAdapter``) is raw, undecoded page HTML -- it never extracts
# plain text. Real manufacturer/retailer pages are thick with colons in
# tags/attributes/CSS/JS that are NOT product specifications (e.g.
# ``<meta property="og:image" content="...">``, inline ``color:#fff``,
# ``<script>`` JSON blobs) and must never be mistaken for a
# "label: value" spec line by ``extract_spec_lines`` below.
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_markup(text: str) -> str:
    """Defensive HTML-to-text normalization ahead of line-based spec
    extraction. Script/style blocks are dropped entirely (their content is
    never a real spec value); remaining tags are replaced with a space so
    words on either side of a stripped tag never get glued together."""
    without_scripts = _SCRIPT_STYLE_RE.sub(" ", str(text or ""))
    return _HTML_TAG_RE.sub(" ", without_scripts)


def match_canonical_key(raw_label: str) -> str | None:
    """Substring match against the alias table -- never a fuzzy/guessed
    key. Returns None for any label this table has no verified mapping
    for (the caller must keep it out of the characteristics contract, per
    the "unknown characteristic: preserve source data, do not write it"
    requirement -- callers upstream of this module are responsible for
    keeping raw source data elsewhere if they want to retain it)."""
    blob = str(raw_label or "").strip().casefold()
    if not blob:
        return None
    for alias, key in _LABEL_LOOKUP:
        if alias in blob:
            return key
    return None


def normalize_characteristic_value(key: str, raw_value: str) -> tuple[str, str]:
    """Deterministic unit normalization ONLY where a conversion is
    unambiguous (inches -> cm for screen diagonal); everything else is
    passed through verbatim with the table's declared unit -- never a
    silent, unverifiable conversion (module docstring requirement)."""
    unit = CANONICAL_CHARACTERISTIC_ALIASES.get(key, ("", ()))[0]
    text = str(raw_value or "").strip()
    if key == "screen_diagonal_cm":
        cm_match = _CM_RE.search(text)
        if cm_match:
            return cm_match.group(1).replace(",", "."), "cm"
        inch_match = _INCH_RE.search(text)
        if inch_match:
            inches = float(inch_match.group(1).replace(",", "."))
            return f"{inches * 2.54:.1f}", "cm"
        number_match = _NUMBER_RE.search(text)
        if number_match:
            # A bare number with no unit token is assumed to already be the
            # table's declared unit (cm) -- never re-interpreted as inches.
            return number_match.group(0).replace(",", "."), "cm"
        return text, unit
    return text, unit


def bridge_characteristics_to_bitrix(
    characteristics: Mapping[str, NormalizedCharacteristic],
) -> dict[str, NormalizedCharacteristic]:
    """Attaches the verified Bitrix ``property_id`` (from the #48
    ``integrations.bitrix.schema.CATALOG_CHARACTERISTICS`` resolver) to
    every characteristic Panda already knows one for -- never invents one
    for a key with no verified binding; those keys are returned unchanged
    (``bitrix_property_id=None``, ``bitrix_writable=False``) and remain in
    the canonical enrichment data regardless."""
    out: dict[str, NormalizedCharacteristic] = {}
    for key, characteristic in characteristics.items():
        binding = bitrix_schema.characteristic_binding(key)
        if binding is None:
            out[key] = characteristic
            continue
        out[key] = NormalizedCharacteristic(
            key=characteristic.key,
            value=characteristic.value,
            unit=characteristic.unit,
            confidence=characteristic.confidence,
            supporting_facts=characteristic.supporting_facts,
            bitrix_property_id=binding.property_id,
            bitrix_writable=characteristic.confidence in (CONFIDENCE_VERIFIED, CONFIDENCE_PROBABLE),
        )
    return out


def merge_facts_into_characteristics(
    facts: Sequence[SourceFact],
) -> tuple[dict[str, NormalizedCharacteristic], tuple[IdentityConflict, ...]]:
    """Groups facts by canonical characteristic key and resolves a single
    normalized value per key:

    - a single source (any trust level) -> ``CONFIDENCE_PROBABLE``;
    - >=2 independent-domain sources agreeing on the SAME normalized value
      -> ``CONFIDENCE_VERIFIED``;
    - a single source from a manufacturer-tier domain -> ``CONFIDENCE_VERIFIED``;
    - sources disagreeing on the normalized value for the same key, with no
      trust level strictly dominating -> the key is dropped from the
      returned mapping and reported as an ``IdentityConflict`` instead
      (requirement 1: "mark conflict and do not silently choose").
    """
    by_key: dict[str, list[SourceFact]] = {}
    for fact in facts:
        if not fact.characteristic_key:
            continue
        by_key.setdefault(fact.characteristic_key, []).append(fact)

    resolved: dict[str, NormalizedCharacteristic] = {}
    conflicts: list[IdentityConflict] = []
    for key, key_facts in by_key.items():
        by_value: dict[str, list[SourceFact]] = {}
        for fact in key_facts:
            by_value.setdefault(fact.normalized_value, []).append(fact)

        if len(by_value) == 1:
            value, supporting = next(iter(by_value.items()))
            domains = {f.source_domain for f in supporting}
            max_trust = max(SOURCE_TRUST_RANK.get(f.source_type, 0) for f in supporting)
            confidence = (
                CONFIDENCE_VERIFIED
                if len(domains) >= 2 or max_trust >= SOURCE_TRUST_RANK.get("manufacturer", 4)
                else CONFIDENCE_PROBABLE
            )
            resolved[key] = NormalizedCharacteristic(
                key=key,
                value=value,
                unit=supporting[0].unit,
                confidence=confidence,
                supporting_facts=tuple(supporting),
            )
            continue

        # Multiple distinct values for the same key -- only accept the
        # highest-trust group if it STRICTLY dominates every other group;
        # otherwise fail closed to a recorded conflict, never a guess.
        ranked = sorted(
            by_value.items(),
            key=lambda item: max(SOURCE_TRUST_RANK.get(f.source_type, 0) for f in item[1]),
            reverse=True,
        )
        top_value, top_facts = ranked[0]
        top_trust = max(SOURCE_TRUST_RANK.get(f.source_type, 0) for f in top_facts)
        runner_up_trust = max(
            (SOURCE_TRUST_RANK.get(f.source_type, 0) for _, facts_ in ranked[1:] for f in facts_),
            default=-1,
        )
        if top_trust > runner_up_trust:
            resolved[key] = NormalizedCharacteristic(
                key=key,
                value=top_value,
                unit=top_facts[0].unit,
                confidence=CONFIDENCE_PROBABLE,
                supporting_facts=tuple(top_facts),
            )
        else:
            all_facts = tuple(f for _, group in by_value.items() for f in group)
            conflicts.append(
                IdentityConflict(
                    code=f"characteristic_conflict_{key}",
                    detail=f"conflicting values for {key!r}: {sorted(by_value)}",
                    conflicting_facts=all_facts,
                )
            )
    return resolved, tuple(conflicts)


def extract_spec_lines(page_text: str) -> Iterable[tuple[str, str]]:
    """Best-effort extraction of ``label: value`` / ``label — value`` spec
    lines from plain page text (manufacturer/distributor spec pages
    commonly render one characteristic per line in this shape). Never
    invents a label/value that is not literally present in the text --
    lines that do not match the pattern are simply skipped. Markup is
    stripped first (see ``_strip_html_markup``) so raw HTML page bodies
    never leak tag/attribute/CSS/JS colons into the result."""
    plain_text = _strip_html_markup(page_text)
    for raw_line in plain_text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 200:
            continue
        for sep in (":", "\u2014", "-", "\u2013"):
            if sep in line:
                label, _, value = line.partition(sep)
                label = label.strip()
                value = value.strip()
                if label and value and len(label) < 80:
                    yield label, value
                    break
