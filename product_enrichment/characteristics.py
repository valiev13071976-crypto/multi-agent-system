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

import html as html_module
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
    "package_dimensions": ("mm", ("габариты упаковки", "package dimensions", "carton dimensions", "shipping dimensions")),
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

# Unit tokens that are SYNONYMS of a canonical key's own declared unit --
# a value like "120 Гц" for ``refresh_rate_hz`` (declared unit ``Hz``)
# already states that unit, so keeping the token duplicated it downstream
# ("частота обновления 120 Гц Гц"). Stripping a synonym token is not a
# conversion: the number is unchanged and stays in its declared unit.
_UNIT_SYNONYMS: Mapping[str, tuple[str, ...]] = {
    "Hz": ("гц", "hz"),
    "W": ("вт", "w"),
    "kg": ("кг", "kg"),
    "cm": ("см", "cm"),
    "mm": ("мм", "mm"),
}
# Cyrillic display suffix for each internal unit code -- used ONLY by
# ``format_value_with_unit`` below (content rendering); the internal code
# itself (e.g. "Hz") is never shown to a user.
_UNIT_DISPLAY_SUFFIX: Mapping[str, str] = {
    "Hz": "Гц",
    "W": "Вт",
    "kg": "кг",
    "cm": "см",
    "mm": "мм",
}


def format_value_with_unit(key: str, value: str) -> str:
    """Appends ``key``'s declared unit display suffix to ``value`` for
    preview/detail content rendering (TV characteristic normalization
    quality defect closure, production preview follow-up to PR #85) --
    UNLESS ``value`` already states that unit inline (its own synonym
    tokens, e.g. "Гц"/"Hz" for the ``Hz`` unit), which a normalized value
    legitimately can (e.g. ``refresh_rate_hz = "120Гц (VRR 165Гц)"``
    already carries its own unit). Never produces a duplicated unit
    suffix such as "120 Гц Гц", "30 кг кг" or "254 см см"."""
    text = str(value or "")
    unit = CANONICAL_CHARACTERISTIC_ALIASES.get(key, ("", ()))[0]
    suffix = _UNIT_DISPLAY_SUFFIX.get(unit, "")
    if not suffix:
        return text
    already_present_tokens = {suffix.casefold(), unit.casefold(), *_UNIT_SYNONYMS.get(unit, ())}
    lowered = text.casefold()
    if any(token and token in lowered for token in already_present_tokens):
        return text
    return f"{text} {suffix}"


_NUMBER_WITH_UNIT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([^\d\s]{1,3})?\.?$", re.UNICODE)

_INCH_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:\"|inch|inches|дюйм)", re.I)
_CM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:cm|см)", re.I)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# TV characteristic normalization quality defect closure (production
# preview, LG 100MRGB96B6.ARUG -- ticket follow-up to PR #85): a "count"
# semantic type (currently ``hdmi_count``/``usb_count`` -- generalized by
# key SUFFIX so any future ``*_count`` canonical key gets the same
# guarantee for free) must resolve to an actual small integer count, never
# survive as unrelated extracted text ("вход"/"камеры"/"есть"/"да") that
# happened to sit next to a recognized "HDMI"/"USB" label on a real page.
# A bare digit run anywhere in the text is accepted (covers "4", "3 x
# HDMI 2.1", "2 (USB-A + USB-C)"); anything with no digit at all, or an
# implausibly large "count" (a mis-extracted model number/year), is
# rejected -- never guessed.
_COUNT_KEY_SUFFIX = "_count"
_COUNT_NUMBER_RE = re.compile(r"\d+")
_MAX_PLAUSIBLE_PORT_COUNT = 32


def _extract_port_count(text: str) -> str:
    match = _COUNT_NUMBER_RE.search(text)
    if not match:
        return ""
    digits = match.group(0)
    # A run longer than two digits is never a plausible port count (it is
    # far more likely a mis-extracted model number/year) -- reject rather
    # than truncate it into a fabricated small count.
    if len(digits) > 2:
        return ""
    count = int(digits)
    if count < 1 or count > _MAX_PLAUSIBLE_PORT_COUNT:
        return ""
    return str(count)


# Physical product colour acceptance (same defect closure): real catalog
# pages sometimes mislabel a display-technology/marketing sentence (e.g.
# "Основные цвета RGB Ультра (Тройная 100% сертификация цвета)") as the
# value for a "Цвет"/"Color" spec row. A genuine physical colour is a
# short phrase built from a small, closed vocabulary of colour words
# (optionally with a finish qualifier like "металлик"/"matte") -- never a
# multi-clause marketing sentence with digits, percentages or parenthetical
# qualifiers. This is a generic vocabulary check (not product-specific):
# any value outside this shape is rejected rather than guessed.
_KNOWN_COLOR_WORDS = frozenset(
    {
        # Russian base hues + common finishes
        "черный", "чёрный", "белый", "серый", "серебристый", "серебряный",
        "золотой", "золотистый", "синий", "голубой", "красный", "бордовый",
        "зеленый", "зелёный", "желтый", "жёлтый", "розовый", "фиолетовый",
        "сиреневый", "оранжевый", "коричневый", "бежевый", "бронзовый",
        "графитовый", "титановый", "медный", "стальной", "антрацит",
        "хром", "хромированный", "перламутровый", "металлик", "матовый",
        "глянцевый", "космический",
        # English base hues + common finishes
        "black", "white", "grey", "gray", "silver", "gold", "golden",
        "blue", "navy", "red", "maroon", "green", "yellow", "pink",
        "purple", "violet", "orange", "brown", "beige", "bronze",
        "graphite", "titanium", "copper", "steel", "charcoal", "chrome",
        "pearl", "space", "matte", "glossy", "metallic",
    }
)
_COLOR_REJECT_CHARS_RE = re.compile(r"[\d%()]")
_COLOR_WORD_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)
_MAX_COLOR_VALUE_CHARS = 40
_MAX_COLOR_WORDS = 4


def _is_plausible_physical_color(text: str) -> bool:
    if not text or len(text) > _MAX_COLOR_VALUE_CHARS:
        return False
    if _COLOR_REJECT_CHARS_RE.search(text):
        return False
    words = [w for w in _COLOR_WORD_SPLIT_RE.split(text.casefold()) if w]
    if not words or len(words) > _MAX_COLOR_WORDS:
        return False
    return any(word in _KNOWN_COLOR_WORDS for word in words)

# ``scrape.fetch``'s ``body_text`` (``tools.platform.web_fetch_adapter.
# WebFetchAdapter``) is raw, undecoded page HTML -- it never extracts
# plain text. Real manufacturer/retailer pages are thick with colons in
# tags/attributes/CSS/JS that are NOT product specifications (e.g.
# ``<meta property="og:image" content="...">``, inline ``color:#fff``,
# ``<script>`` JSON blobs) and must never be mistaken for a
# "label: value" spec line by ``extract_spec_lines`` below.
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")
_ANCHOR_OPEN_RE = re.compile(r"<a\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"[\s\u00a0]+")


def _strip_html_markup(text: str) -> str:
    """Defensive HTML-to-text normalization ahead of line-based spec
    extraction. Script/style blocks are dropped entirely (their content is
    never a real spec value); remaining tags are replaced with a space so
    words on either side of a stripped tag never get glued together."""
    without_scripts = _SCRIPT_STYLE_RE.sub(" ", str(text or ""))
    return _HTML_TAG_RE.sub(" ", without_scripts)


def html_text_nodes(text: str) -> tuple[tuple[str, bool], ...]:
    """Splits a page body into its ordered, non-empty TEXT NODES, each
    paired with whether it is anchor (``<a>``) text.

    A tag boundary is a text-node boundary: real product pages put a
    characteristic's label and its value in SEPARATE elements (table
    cells, ``<dt>``/``<dd>``, or nested ``<div>``/``<span>`` pairs), so
    markup must SPLIT the two, never glue them into one string. Character
    entities are decoded and whitespace (including ``&nbsp;``) collapsed,
    so values such as ``55&quot;`` / ``120&nbsp;Гц`` are the literal text
    a reader sees. Plain-text pages simply come back as their own lines."""
    without_scripts = _SCRIPT_STYLE_RE.sub(" ", str(text or ""))
    nodes: list[tuple[str, bool]] = []
    in_anchor = False
    for chunk in _HTML_TAG_SPLIT_RE.split(without_scripts):
        if not chunk:
            continue
        if chunk.startswith("<") and chunk.endswith(">"):
            if _ANCHOR_OPEN_RE.match(chunk):
                in_anchor = True
            elif chunk.casefold().startswith("</a"):
                in_anchor = False
            continue
        for raw_line in html_module.unescape(chunk).splitlines():
            line = _WHITESPACE_RE.sub(" ", raw_line).strip()
            if line:
                nodes.append((line, in_anchor))
    return tuple(nodes)


# A recognized alias must actually BE the label, not merely occur
# somewhere inside a long unrelated string. Real catalog pages are full of
# marketing/cross-sell text that happens to contain a characteristic word
# (e.g. a related-product line 'Телевизор LG 65" OLED65G5RLA.ARUG (Цвет'
# followed by its price) -- accepting those as a "Цвет" label produced
# garbage facts which then collided with the real value and got dropped as
# a conflict, leaving zero characteristics.
_SHORT_LABEL_MAX_CHARS = 24
_MIN_ALIAS_COVERAGE = 0.4


def _alias_is_significant(alias: str, label: str) -> bool:
    return len(label) <= _SHORT_LABEL_MAX_CHARS or len(alias) >= _MIN_ALIAS_COVERAGE * len(label)


def match_canonical_key(raw_label: str) -> str | None:
    """Substring match against the alias table -- never a fuzzy/guessed
    key. Returns None for any label this table has no verified mapping
    for (the caller must keep it out of the characteristics contract, per
    the "unknown characteristic: preserve source data, do not write it"
    requirement -- callers upstream of this module are responsible for
    keeping raw source data elsewhere if they want to retain it).

    A match additionally requires the alias to be significant relative to
    the whole label (``_alias_is_significant``), so a characteristic word
    buried in a long sentence is not mistaken for that characteristic's
    label."""
    blob = str(raw_label or "").strip().casefold()
    if not blob:
        return None
    for alias, key in _LABEL_LOOKUP:
        if alias in blob and _alias_is_significant(alias, blob):
            return key
    return None


_SHORT_ENUM_KEYS = frozenset(
    {
        "panel_technology",
        "backlight_technology",
        "operating_system",
        "hdr_formats",
        "tuners",
        "smart_tv_support",
        "wifi_support",
        "bluetooth_support",
        "ethernet_support",
        "country_of_origin",
    }
)
_MAX_SHORT_ENUM_CHARS = 72
_MAX_SHORT_ENUM_WORDS = 10
_SENTENCE_PUNCT_RE = re.compile(r"[.!?;]")
_DIMENSION_VALUE_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:x|×|х)\s*\d+(?:[.,]\d+)?\s*(?:x|×|х)\s*\d+(?:[.,]\d+)?(?:\s*(?:mm|мм|cm|см))?\s*$",
    re.I,
)
_VESA_VALUE_RE = re.compile(
    r"^\s*\d{2,4}\s*(?:x|×|х)\s*\d{2,4}(?:\s*(?:mm|мм))?\s*$",
    re.I,
)
_RESOLUTION_VALUE_RE = re.compile(
    r"^\s*(?:\d{3,5}\s*(?:x|×|х)\s*\d{3,5}|(?:hd|full\s*hd|fhd|uhd|4k|8k)(?:\s+uhd)?)\s*$",
    re.I,
)


def _is_short_spec_value(text: str) -> bool:
    """Generic guard for enum-like catalog values.

    Real values such as ``OLED``, ``Mini LED``, ``Google TV`` or
    ``HDR10+, Dolby Vision, HLG`` are compact tokens/phrases. A long
    prose sentence beside a recognized label is usually marketing copy
    accidentally paired by structural HTML extraction and must not become
    a product characteristic.
    """
    value = _WHITESPACE_RE.sub(" ", str(text or "")).strip()
    if not value or len(value) > _MAX_SHORT_ENUM_CHARS:
        return False
    if len(value.split()) > _MAX_SHORT_ENUM_WORDS:
        return False
    if _SENTENCE_PUNCT_RE.search(value):
        return False
    return True


def _passes_characteristic_value_shape(key: str, text: str) -> bool:
    """Fail-closed shape validation before a sourced value becomes a fact.

    This is category/model/brand agnostic. It validates the semantic SHAPE
    of canonical fields, never guesses a replacement value.
    """
    value = _WHITESPACE_RE.sub(" ", str(text or "")).strip()
    if not value:
        return False
    if key in _SHORT_ENUM_KEYS:
        if not _is_short_spec_value(value):
            return False
        if key == "country_of_origin" and any(ch.isdigit() for ch in value):
            return False
    if key in {"dimensions_with_stand", "dimensions_without_stand", "package_dimensions"}:
        return bool(_DIMENSION_VALUE_RE.match(value))
    if key == "vesa_mount":
        return bool(_VESA_VALUE_RE.match(value))
    if key == "screen_resolution":
        return bool(_RESOLUTION_VALUE_RE.match(value))
    if key == "model_year":
        match = re.fullmatch(r"20\d{2}", value)
        return bool(match) and 2000 <= int(value) <= 2100
    return True

def normalize_characteristic_value(key: str, raw_value: str) -> tuple[str, str]:
    """Deterministic unit normalization ONLY where a conversion is
    unambiguous (inches -> cm for screen diagonal); everything else is
    passed through verbatim with the table's declared unit -- never a
    silent, unverifiable conversion (module docstring requirement)."""
    unit = CANONICAL_CHARACTERISTIC_ALIASES.get(key, ("", ()))[0]
    text = str(raw_value or "").strip()
    if not _passes_characteristic_value_shape(key, text):
        return "", unit
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
    if key == "color":
        return (text, unit) if _is_plausible_physical_color(text) else ("", unit)
    if key.endswith(_COUNT_KEY_SUFFIX):
        return _extract_port_count(text), unit
    synonyms = _UNIT_SYNONYMS.get(unit)
    if synonyms:
        match = _NUMBER_WITH_UNIT_RE.match(text)
        if match and (match.group(2) or "").casefold() in ("", *synonyms):
            return match.group(1).replace(",", "."), unit
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


_MAX_INLINE_LINE_CHARS = 200
_MAX_STRUCTURAL_LABEL_CHARS = 60
_MAX_STRUCTURAL_VALUE_CHARS = 120
_LABEL_TRAILING_CHARS = " \t:\u2014\u2013-\u00a0"


def _inline_pair(line: str) -> tuple[str, str] | None:
    for sep in (":", "\u2014", "-", "\u2013"):
        if sep in line:
            label, _, value = line.partition(sep)
            label = label.strip()
            value = value.strip()
            if label and value and len(label) < 80:
                return label, value
    return None


def _plausible_structural_value(value: str) -> bool:
    if not value or len(value) > _MAX_STRUCTURAL_VALUE_CHARS:
        return False
    if not any(ch.isalnum() for ch in value):
        return False
    # A trailing separator marks the text as the NEXT label (a value-less
    # label, e.g. one whose value is a colour swatch), never as a value.
    if value.endswith((":", "\u2014", "\u2013")):
        return False
    # A second recognized LABEL is never the first one's value (e.g. a
    # column of labels followed by a column of values).
    return match_canonical_key(value.strip(_LABEL_TRAILING_CHARS)) is None


def extract_spec_lines(page_text: str) -> Iterable[tuple[str, str]]:
    """Best-effort extraction of ``(label, value)`` characteristic pairs
    from one fetched page, in the two shapes real sources actually use:

    1. ``label: value`` / ``label — value`` on a single line -- plain-text
       spec sheets and pages that render a characteristic as one string;
    2. STRUCTURAL pairs -- a text node whose text is a recognized
       characteristic label (``match_canonical_key``) immediately followed
       by the next text node, which is its value. This is how every real
       catalog page renders specifications: ``<th>label</th><td>value</td>``,
       ``<dt>label</dt><dd>value</dd>`` or nested ``<div>``/``<span>``
       pairs. Before this, only shape 1 was supported, so a real product
       page (whose markup carries no colon between label and value, and
       whose minified body has no line breaks either) produced ZERO
       characteristics.

    Never invents a label/value that is not literally present in the page:
    labels must match the verified alias table and values are the adjacent
    text node verbatim. Text rendered as link text is skipped in both
    shapes (site navigation and cross-sell blocks, e.g. a
    "HDMI-кабели" category link, are never specifications)."""
    nodes = html_text_nodes(page_text)
    inline_pair_indexes: set[int] = set()
    for index, (line, in_anchor) in enumerate(nodes):
        if in_anchor or len(line) > _MAX_INLINE_LINE_CHARS:
            continue
        pair = _inline_pair(line)
        if pair is not None:
            inline_pair_indexes.add(index)
            yield pair

    for index, (line, in_anchor) in enumerate(nodes):
        if index in inline_pair_indexes or in_anchor:
            continue
        if len(line) > _MAX_STRUCTURAL_LABEL_CHARS:
            continue
        label = line.strip(_LABEL_TRAILING_CHARS)
        if not label or match_canonical_key(label) is None:
            continue
        if index + 1 >= len(nodes):
            continue
        value = nodes[index + 1][0].strip()
        if _plausible_structural_value(value):
            yield label, value
