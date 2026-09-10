"""Real production schema binding for panda.msk.ru (Block 5.6).

This module binds Panda's vendor-neutral Bitrix connector to the REAL,
installed 1C-Bitrix "Управление сайтом 26.700.0" + Aspro Premier schema for
this specific site, WITHOUT inventing a second connector/architecture:

    Catalog product IBLOCK  = 14  (``BitrixIntegrationConfig.catalog_id``)
    Offers/SKU IBLOCK        = 15  (``BitrixIntegrationConfig.offers_iblock_id``)

The property IDs/codes below are this installation's REAL, already-observed
schema (verification fixtures per the Block 5.6 spec, section 3/4) -- they
are NOT a generic assumption about "all Bitrix catalogs" or "all products".
Nothing here hardcodes product-level business logic (no "every product is a
TV", no "every product is in section 70"); category/section resolution and
per-product property values always come from the live/fixture READ, never
from this table. This table only says: *if* property 100 appears on a
product, its Bitrix CODE is ``BRAND`` and Panda owns writing it; it never
asserts that any particular product has that property populated.

IMPORTANT -- this table is intentionally NOT a complete inventory of the
live IBLOCK 14/15 property set. A follow-up LIVE READ-ONLY discovery pass
(direct read-only ``catalog.product.list``/``catalog.product.offer.list``
calls against the real webhook) found this installation actually exposes
roughly 179 distinct custom property IDs on IBLOCK 14 (97 through 277) and
up to property 298 on IBLOCK 15 -- far more than the ~20 entries below.
Only properties whose semantic meaning has actually been verified are
listed here; every other live property id (137-277 on IBLOCK 14, 298 on
IBLOCK 15, and any future addition) correctly falls through
``property_ownership()``'s conservative default of ``UNMANAGED_PRESERVE``
below -- never assumed writable, never guessed a CODE/name. Bitrix's REST
surface for this installation has no working ``iblock.property.list``
(returns ``ERROR_METHOD_NOT_FOUND``), so CODE names for those undocumented
properties cannot currently be verified via REST at all; adding entries
for them would mean guessing, which this binding deliberately never does.

Ownership classification (spec sections 16/31/32/Acceptance W) is
authoritative here and reused verbatim -- no separate/weaker taxonomy:

    PANDA_MANAGED       Panda's canonical Product model owns this field.
    BITRIX_MANAGED      Owned by Bitrix/site editors; Panda only reads it.
    ASPRO_MANAGED       Aspro Premier theme content (banners, buttons, ...);
                        Panda never writes Aspro-specific presentation.
    DERIVED             Computed by Bitrix itself from other data (e.g. a
                        MIN/MAX price rollup) -- never written directly.
    READ_ONLY           Read-only identity/relationship data.
    UNMANAGED_PRESERVE  Exists on the installation but outside this
                        integration's boundary; must never be touched.

REST method note (bounded evidence, no live call needed to establish this --
these are the documented, scope-`catalog`/`iblock` Bitrix REST contracts):

    catalog.product.list        -- product identity/content/characteristics
                                    (``select`` uses ``propertyN`` for a
                                    custom property with numeric ID N, e.g.
                                    property ID 100 -> ``property100``).
    catalog.section.list        -- section/category tree (``iblockSectionId``
                                    on a section is its PARENT section id;
                                    hierarchy is resolved by walking this,
                                    never assumed).
    catalog.product.offer.list  -- offers/SKUs; the underlying CML2_LINK
                                    (property 279) parent-product link is
                                    exposed by this REST method as the
                                    ``parentId`` filter/select field, not a
                                    raw ``property279`` value.
    catalog.price.list          -- prices, one row per (productId,
                                    catalogGroupId) pair; catalogGroupId is
                                    the price-type identifier (see
                                    catalog.priceType.list) -- MSC/EKB/
                                    MAGNITOGORSK style regional prices are
                                    distinct rows, never collapsed.

SEO note (spec section 7): the standard ``catalog_product`` REST object does
NOT expose IPROPERTY-style *inherited/effective* SEO (the section->iblock
->site template chain is a server-side PHP computation, not a plain field).
Only explicit product-level SEO overrides -- if this installation's schema
ever exposes one as an ordinary property -- would be readable this way; the
*effective* value is a genuine, currently-unresolved REST-surface gap, not a
missing scope. A follow-up LIVE READ-ONLY discovery pass confirmed this is
even stronger than originally documented: this installation's real
``catalog.product.list``/``catalog.product.offer.list`` responses carry no
seo/meta/title/description/keyword-shaped key at all (not even present as
``null``) -- there is currently no writable REST destination for SEO
title/description/keywords here, explicit or inherited.
``seo_effective_status`` below reports this precisely instead
of fabricating a value (spec section 10: "classify the exact limitation").

Purchase price note (Block 5.6 follow-up defect closure): unlike EAN/GTIN
(which still has NO verified Bitrix destination on this installation --
never invented), the same LIVE discovery pass confirmed
``catalog.product.list``/``catalog.product.offer.list`` responses include
two NATIVE, first-class Bitrix catalog fields -- ``purchasingPrice`` and
``purchasingCurrency`` -- distinct from the custom ``propertyN`` table
above (they are ordinary top-level REST fields, not IBLOCK properties, so
they intentionally have no ``PropertyBinding`` entry). They were observed
present but unpopulated (``null``) on every sampled live product -- this
installation has never used them yet, but the destination itself is real
and verified. See ``PURCHASING_PRICE_FIELD``/``PURCHASING_CURRENCY_FIELD``
below and ``LiveBitrixAdapter._write_product_create_live`` for the write
mapping. Purchase price remains structurally independent from the retail
selling price (``catalog.price.add``/``BITRIX_RETAIL_PRICE_TYPE_ID``) --
neither can ever substitute for the other.

Property-value envelope note (Block 5.6 follow-up defect closure): LIVE
``catalog.product.list``/``catalog.product.offer.list`` responses wrap
most non-boolean custom property values as ``{"value": ..., "valueId":
...}`` (or an array of such envelopes for multi-value properties) rather
than a bare scalar -- confirmed for BRAND (property 100), ARTICLE
(property 283), and the CML2_LINK/``parentId`` relationship itself.
``unwrap_property_value`` below centralizes extracting the real value from
either shape (envelope or already-scalar, for backward compatibility
with older/fixture responses); every mapping function in this module
routes through it instead of each doing its own ad-hoc unwrap.

Complete-product-card follow-up pass (second Block 5.6 follow-up defect
closure -- "skeleton product card" defect, real products 992/993): a
second bounded LIVE READ-ONLY discovery pass (real webhook, read-only
``catalog.section.list``/``catalog.product(.offer).list`` explicit-select
calls, and -- newly discovered this pass -- ``catalog.productProperty.list``/
``.get``, which DOES work on this installation and was not tried before)
resolved most of the still-missing product-card data:

  A. SECTION ("Телевизоры") -- ``catalog.section.list`` (filter
     ``iblockId=14``) confirms exactly one section named "Телевизоры":
     id 70, code ``televizory``, parent ``iblockSectionId=61`` (section 61
     = "Электроника", a root section with no parent). Chain: Электроника
     (61) -> Телевизоры (70). Three sub-sections exist under it (71 FULL
     HD, 72 Смарт-телевизоры, 73 "с изогнутым экраном" -- the section the
     real verification product 477 happens to live in) but "Телевизоры"
     itself (70) is the unambiguous top-level match for the literal name.
     ``iblockSectionId`` is a documented, real ``catalog.product.add``
     field (Bitrix's own REST reference lists it directly in the ``fields``
     example) and is exactly the same field this module already reads
     (``map_catalog_product``'s ``category.section_id``) -- a genuinely
     symmetric read/write destination. See ``SECTION_FIELD``,
     ``resolve_section_id`` below; the *resolution* (Panda category/
     subcategory name -> this id) is intentionally never hardcoded here
     (no "TV products always get id 70") -- callers must resolve
     dynamically against a live/fixture ``catalog.section.list`` snapshot
     and fail closed on an ambiguous/missing match, never guess or default
     to catalog root.

  B. EAN/BARCODE -- still NO verified destination. This pass additionally
     ruled out several plausible REST method names (all confirmed
     ``ERROR_METHOD_NOT_FOUND``/404 on this installation's granted webhook
     scopes): ``catalog.productBarcode.list/.get``, ``catalog.barcode.list``,
     ``catalog.product.barcode.list``, ``catalog.storeBarcode.list``,
     ``catalog.document.barcode.list``, ``crm.product.list``/``.fields``
     (the Bitrix24 CRM catalog does not exist on this self-hosted
     install). An explicit ``select=["barcode"]`` on both
     ``catalog.product.list`` and ``catalog.product.offer.list`` is
     silently dropped (not a real field on either entity). The only
     barcode-shaped thing found is ``WB_BARCODE`` (offer-adjacent IBLOCK 14
     property 273, "WB: Штрих-код номенклатуры") -- explicitly scoped to a
     Wildberries marketplace-sync integration (properties 268-274 are all
     "WB: ..." prefixed), not a general-purpose EAN field; using it for
     Panda's own EAN would be exactly the kind of arbitrary-property
     misuse this binding refuses to do. EAN remains sourced-but-unwritten.

  C. CHARACTERISTICS -- ``catalog.productProperty.list``/``.get`` (a
     DIFFERENT REST method family from the previously-tried and still-
     unavailable ``iblock.property.list``) works on this installation and
     returns full property metadata: real Russian admin ``name``,
     ``propertyType`` (``S``/``N``/``L``/``E``/``F``), ``multiple``,
     ``userType``, for all 181 IBLOCK 14 + 21 IBLOCK 15 properties. Cross-
     referencing the requested example characteristics against that real
     metadata found exactly five with an unambiguous, verified semantic
     match (see ``CATALOG_CHARACTERISTICS`` below); "display technology",
     "refresh rate" and "model/year" have no matching property on this
     installation and are deliberately left unmapped (Panda may still
     carry that source data, it is just never written to a guessed
     property). Every OTHER live property not listed in
     ``CATALOG_CHARACTERISTICS``/``CATALOG_PRODUCT_PROPERTIES`` remains
     ``UNMANAGED_PRESERVE`` exactly as before -- this pass does not attempt
     to manage all 181 properties, only the ones a real admin-panel label
     unambiguously confirms.

  D. WEIGHT/DIMENSIONS -- ``weight``/``width``/``length``/``height`` are
     confirmed REAL, selectable, native (non-property) fields on both
     ``catalog.product.list``/``.get`` and ``catalog.product.offer.list``
     (present, though null on every sampled live row, only when explicitly
     named in ``select`` -- like ``id``/``iblockId``, they are NOT included
     by a bare ``"*"`` wildcard select). Bitrix's own official REST
     reference (``catalog.product.add``/data-types pages) lists all four
     as real, documented, writable fields but -- confirmed by directly
     reading that reference text -- does NOT state their unit anywhere
     (just "double"/"float", "Weight of the product"). No non-null live
     value exists on this installation to cross-check empirically either.
     This is a genuine, confirmed documentation gap, not a guess: the
     mapping below passes these through as opaque numbers with ZERO
     conversion (never silently converts a unit), and the Panda-facing
     contract makes the assumed unit explicit in the field name itself
     (``weight_g``/``length_mm``/``width_mm``/``height_mm``, following
     Bitrix's long-standing legacy catalog convention of grams/
     millimeters) rather than asserting it silently inside this module.

  E/F. PREVIEW/DETAIL CONTENT -- ``previewText``/``previewTextType``/
     ``detailText``/``detailTextType`` are already confirmed real read+
     write fields (present on every live read; explicitly listed in
     Bitrix's ``catalog.product.add`` reference). ``previewPicture``/
     ``detailPicture`` READ as ``{"id","url","urlMachine"}``; Bitrix's own
     ``catalog.product.add`` reference additionally documents the WRITE
     shape for both: ``{"fileData": ["<filename>", "<base64 content>"]}``.
     No confirmed write-format example exists for generic multi-value FILE
     properties (e.g. MORE_PHOTO/124 on IBLOCK 14, MORE_PHOTO/280 on
     IBLOCK 15 -- the Aspro gallery) beyond these two dedicated top-level
     fields, so gallery/additional-image writing remains deferred rather
     than guessed.

  G. SEO -- still no verified writable mechanism. This pass additionally
     ruled out ``iblock.element.get``, ``iblock.elementproperty.list``
     (``ERROR_METHOD_NOT_FOUND``) and ``lists.element.get``
     (``insufficient_scope`` -- exists but this webhook's granted scopes
     do not include it). SEO remains entirely deferred, exactly as before.

See ``SECTION_FIELD``, ``resolve_section_id``, ``CharacteristicBinding``/
``CATALOG_CHARACTERISTICS``/``map_characteristics_to_properties``, and the
native physical/content field constants below for the concrete bindings;
``LiveBitrixAdapter._write_product_create_live`` for the write mapping;
``docs/bitrix-aspro-premier-integration.md`` for the full verification
narrative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def unwrap_property_value(raw):
    """Extract the real value from Bitrix's custom-property envelope shape.

    LIVE ``catalog.product.list``/``catalog.product.offer.list`` responses
    wrap most non-boolean custom properties as ``{"value": ..., "valueId":
    ...}`` for a single value, or a list of such envelopes for a
    multi-value property (e.g. MORE_PHOTO/280, STORES_FILTER/278). Older
    fixtures/tests and boolean/checkbox-type properties (e.g. IN_STOCK/101)
    instead carry a bare scalar directly -- this function is backward
    compatible with that shape too, returning it unchanged.

    Detection is deliberately narrow (a ``dict`` containing a ``"value"``
    key) so it never mistakes an unrelated dict for this envelope -- e.g.
    image/file reference objects such as ``previewPicture``/``detailPicture``
    (``{"id": ..., "url": ..., "urlMachine": ...}``) have no ``"value"``
    key and pass through untouched, exactly as before.
    """
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    if isinstance(raw, list):
        return [unwrap_property_value(item) for item in raw]
    return raw


# Native (non-custom-property) Bitrix ``catalog.product``/``catalog.product
# .offer`` REST fields -- confirmed present (though unpopulated) on this
# installation's real LIVE responses. Ordinary top-level fields, not
# IBLOCK custom properties, so they deliberately have no ``PropertyBinding``
# entry in the tables below; see the module docstring's "Purchase price
# note" for the LIVE evidence and ``LiveBitrixAdapter`` for the write path.
PURCHASING_PRICE_FIELD = "purchasingPrice"
PURCHASING_CURRENCY_FIELD = "purchasingCurrency"

# Native section-assignment field -- confirmed both readable (this module's
# own ``map_catalog_product`` already reads it as ``category.section_id``)
# and writable (listed directly in Bitrix's own ``catalog.product.add``
# REST reference). See the module docstring's item A and
# ``resolve_section_id`` below -- the id itself is NEVER hardcoded/guessed.
SECTION_FIELD = "iblockSectionId"

# Native physical dimension fields -- confirmed real/selectable (module
# docstring item D) but with NO confirmed unit from Bitrix's own REST
# reference or from any non-null live value on this installation. Passed
# through verbatim, never converted; the assumed unit (Bitrix's long-
# standing legacy grams/millimeter convention) is only ever expressed in
# the Panda-facing attribute name (``weight_g``/``length_mm``/etc. on
# ``SingleProductWriteRequest``), never silently assumed inside this
# module.
WEIGHT_FIELD = "weight"
LENGTH_FIELD = "length"
WIDTH_FIELD = "width"
HEIGHT_FIELD = "height"

# Native preview/detail content + media fields (module docstring items
# E/F) -- all confirmed real read+write fields on ``catalog.product.add``.
PREVIEW_TEXT_FIELD = "previewText"
PREVIEW_TEXT_TYPE_FIELD = "previewTextType"
PREVIEW_PICTURE_FIELD = "previewPicture"
DETAIL_TEXT_FIELD = "detailText"
DETAIL_TEXT_TYPE_FIELD = "detailTextType"
DETAIL_PICTURE_FIELD = "detailPicture"
# Confirmed WRITE shape for both picture fields (Bitrix's own
# catalog.product.add REST reference: ``{"fileData": [name, base64]}``).
PICTURE_FILE_DATA_KEY = "fileData"


class SectionResolutionError(Exception):
    """Raised when a Panda category/subcategory name cannot be resolved to
    EXACTLY ONE existing live/fixture Bitrix section -- callers must fail
    closed (require clarification) rather than ever defaulting to catalog
    root or guessing among ambiguous candidates (module docstring item A)."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


# Production defect closure (no_matching_section_found on a HTTP 200
# catalog.section.list): a supplier price list names its categories in its
# OWN vocabulary/language ("TV", "Television", "Телевизор"), while the
# installation's sections are the shop's own names ("Телевизоры"), so an
# exact string equality between the two essentially never matches and every
# product failed closed before any write. These two generic, installation-
# independent layers close that gap WITHOUT ever fuzzy-matching (which
# could silently land a product in a wrong section):
#
#   1. ``_normalize_section_term`` -- casefold, punctuation -> space,
#      collapse whitespace and strip one trailing plural marker per token,
#      so "Телевизоры" and "Телевизор" (or "TVs" and "TV") are the same
#      term. Whole-term comparison only, never substring: "Телевизоры и
#      видео" stays a different term than "Телевизоры".
#   2. ``CATEGORY_CONCEPT_ALIASES`` -- a small, generic cross-language
#      vocabulary of product-category concepts. A candidate and a section
#      match when both normalize to the SAME concept. Vocabulary this table
#      does not know simply falls through and fails closed exactly as
#      before -- nothing is ever guessed, and no section id, brand, SKU or
#      EAN is encoded here.
#   3. Whole-token containment (LIVE production follow-up: the real
#      catalog.section.list data proves supplier labels are COMPOUND --
#      "ТВ", "Телевизоры LED", "Электроника / Телевизоры" -- while the
#      shop's own section names are single terms like "Телевизоры", so
#      layers 1-2, which only ever compare a label to a section AS A
#      WHOLE, can never match them). A section matches when its own name
#      (or its concept) equals one of the candidate's contiguous
#      whole-token runs. Still never a substring match: "ТВ-тюнер" does
#      not contain the token "тв".
#   4. Already-prepared product evidence (LIVE production follow-up: the
#      supplier's category column turned out to be an internal code --
#      "CE" -- that names no product category in ANY vocabulary, so no
#      amount of name matching against the 100+ real sections can ever
#      resolve it). When, and only when, the supplier label itself names
#      no product category at all, the concepts evidenced by the PREPARED
#      card (``derive_category_concepts`` over its title and canonical
#      characteristic keys) select the section instead. A supplier label
#      that does name a category this catalog happens to lack keeps
#      failing closed rather than being silently rerouted by the title.
#
# Every layer still requires EXACTLY ONE surviving section, otherwise the
# same fail-closed SectionResolutionError as before. The one exception is
# layer 3, where a compound label may legitimately name a section AND its
# own ancestor ("Электроника / Телевизоры"): when all matches lie on a
# single parent chain of the same ``catalog.section.list`` snapshot, the
# most specific (deepest) one wins instead of failing closed. Matches on
# different branches remain ambiguous.
_PLURAL_MARKERS = ("ами", "ями", "ов", "ев", "ы", "и", "а", "я", "s")


def _normalize_section_term(text: str) -> str:
    blob = re.sub(r"[^\w\s-]+", " ", str(text or "").strip().casefold().replace("ё", "е"))
    tokens = []
    for token in blob.split():
        for marker in _PLURAL_MARKERS:
            if len(token) > 4 and token.endswith(marker):
                token = token[: -len(marker)]
                break
        tokens.append(token)
    return " ".join(tokens)


CATEGORY_CONCEPT_ALIASES: tuple[tuple[str, ...], ...] = (
    ("tv", "tv set", "television", "телевизор", "телевизоры", "телеви", "тв"),
    ("smartphone", "mobile phone", "cellphone", "смартфон", "телефон"),
    ("laptop", "notebook", "ноутбук"),
    ("monitor", "монитор"),
    ("tablet", "планшет"),
    ("headphones", "headset", "earphones", "наушник", "наушники"),
    ("speaker", "soundbar", "audio", "колонка", "колонки", "саундбар", "аудио"),
    ("camera", "фотоаппарат", "камера"),
    ("printer", "принтер"),
    ("refrigerator", "fridge", "холодильник"),
    ("washing machine", "washer", "стиральная машина"),
    ("dishwasher", "посудомоечная машина"),
    ("oven", "духовой шкаф", "духовка"),
    ("vacuum cleaner", "пылесос"),
    ("microwave", "микроволновая печь", "микроволновка"),
    ("air conditioner", "кондиционер"),
    ("smartwatch", "watch", "смарт-часы", "часы"),
    ("console", "game console", "игровая приставка", "приставка"),
    ("accessories", "аксессуар", "аксессуары"),
)

_CONCEPT_BY_TERM: dict[str, str] = {}
for _aliases in CATEGORY_CONCEPT_ALIASES:
    _concept = _aliases[0]
    for _alias in _aliases:
        _CONCEPT_BY_TERM[_normalize_section_term(_alias)] = _concept


def _section_concept(text: str) -> str:
    return _CONCEPT_BY_TERM.get(_normalize_section_term(text), "")


def _token_runs(text: str) -> set[str]:
    """Every contiguous whole-token run of a normalized term, e.g.
    "электроника телевизор" -> {"электроника", "телевизор",
    "электроника телевизор"}.

    A hyphenated supplier token is ALSO offered as its own parts: real
    price lists qualify the category inline ("ЖК-телевизоры", "Смарт-ТВ",
    "LED-телевизоры") where the shop simply calls the section
    "Телевизоры". Only the candidate label is split this way -- section
    names keep their own hyphenated identity, so "Смарт-телевизоры" stays
    a distinct section rather than collapsing into "Телевизоры".
    """
    tokens = _normalize_section_term(text).split()
    runs = {
        " ".join(tokens[start:end])
        for start in range(len(tokens))
        for end in range(start + 1, len(tokens) + 1)
    }
    runs.update(part for token in tokens for part in token.split("-") if part)
    return runs


# Canonical characteristic keys that identify a product TYPE on their own
# (``product_enrichment.characteristics.CANONICAL_CHARACTERISTIC_ALIASES``
# is the vocabulary these come from). Only keys that no other product type
# can carry belong here -- a display diagonal, for instance, says nothing
# about whether the product is a TV or a monitor, so it is deliberately
# absent and contributes no evidence at all.
CHARACTERISTIC_CONCEPT_EVIDENCE: dict[str, str] = {
    "smart_tv_support": "tv",
    "tuners": "tv",
}


def derive_category_concepts(signals) -> set[str]:
    """Product-category concepts evidenced by the ALREADY prepared product
    data (its title and the canonical characteristic keys enrichment
    resolved) -- a pure function over strings, never a search, an LLM call
    or a new classification subsystem. Uses the same
    ``CATEGORY_CONCEPT_ALIASES`` vocabulary section names are matched
    with, so a signal only counts when it names a product category as a
    whole token."""
    concepts: set[str] = set()
    for signal in signals or ():
        text = str(signal or "").strip()
        if not text:
            continue
        evidenced = CHARACTERISTIC_CONCEPT_EVIDENCE.get(text.casefold())
        if evidenced:
            concepts.add(evidenced)
            continue
        for run in _token_runs(text):
            concept = _CONCEPT_BY_TERM.get(run)
            if concept:
                concepts.add(concept)
    return concepts


def _sections_for_concepts(concepts: set[str], sections: list) -> list:
    return [s for s in sections if _section_concept(s.get("name")) in concepts and _section_concept(s.get("name"))]


def _names_a_product_category(text: str) -> bool:
    """Whether the supplier's own label names a product category at all
    ("Холодильники", "LED-телевизоры") as opposed to being an opaque
    internal code that classifies nothing ("CE", "Consumer Electronics",
    "Электроника")."""
    if _section_concept(text):
        return True
    return any(_CONCEPT_BY_TERM.get(run) for run in _token_runs(text))


def _deepest_of_single_lineage(matches: list, sections: list):
    """Return the most specific match when every match is on ONE parent
    chain of this snapshot (a compound label naming a section and its own
    ancestor), otherwise ``None`` -- matches on different branches stay
    ambiguous and fail closed."""
    def parent_of(section):
        return section.get("iblockSectionId") or section.get("parentSectionId")

    parent_by_id = {}
    for section in sections:
        section_id = section.get("id")
        if section_id is not None:
            parent_by_id[str(section_id)] = parent_of(section)

    def ancestors(section) -> set[str]:
        seen: set[str] = set()
        parent = parent_of(section)
        while parent not in (None, "") and str(parent) not in seen:
            seen.add(str(parent))
            parent = parent_by_id.get(str(parent))
        return seen

    deepest = max(matches, key=lambda section: len(ancestors(section)))
    deepest_lineage = ancestors(deepest) | {str(deepest.get("id"))}
    if all(str(section.get("id")) in deepest_lineage for section in matches):
        return deepest
    return None


def resolve_section_id(*, category: str = "", subcategory: str = "", sections: list, signals=()) -> dict:
    """Deterministically resolve a Panda category/subcategory pair to one
    EXISTING Bitrix section id, from an already-fetched live/fixture
    ``catalog.section.list`` snapshot (``sections``: an iterable of
    ``{"id", "name", ...}`` dicts) -- never a live call itself, so it stays
    a pure function like ``resolve_section_ancestors``.

    ``signals`` are strings the caller ALREADY prepared for this product
    (its title, the canonical characteristic keys enrichment resolved) and
    are consulted only as a last resort, when the supplier's own category
    label names no product category at all -- see the layer notes above.

    Uses ``subcategory`` (more specific -- e.g. "Телевизоры") WHENEVER it
    was supplied, falling back to ``category`` ONLY if no subcategory was
    supplied at all -- if a subcategory WAS supplied but does not match
    any existing section, this fails closed immediately rather than
    silently falling back to the broader category and landing the product
    in an unintended parent section. Matching is an EXACT (trimmed, case-
    insensitive) name match -- no fuzzy/partial matching, which would risk
    a wrong section silently. Zero or more than one section sharing that
    exact name both fail closed via ``SectionResolutionError`` instead of
    ever returning a best guess.
    """
    candidate = subcategory.strip() if subcategory and subcategory.strip() else (
        category.strip() if category and category.strip() else ""
    )
    if not candidate:
        raise SectionResolutionError("section_name_not_supplied", "no category/subcategory supplied to resolve")

    evidence_concepts = derive_category_concepts(signals)

    sections = list(sections)
    if not sections:
        # A HTTP 200 that carried no sections at all is a different
        # failure from "this category does not match any of them": it
        # means the configured catalog IBLOCK has no sections (e.g.
        # BITRIX_CATALOG_ID pointing at the offers/services IBLOCK), and
        # no category value could ever resolve against it.
        raise SectionResolutionError(
            "section_list_empty",
            "catalog.section.list returned no sections for the configured catalog IBLOCK",
        )

    by_name: dict[str, list] = {}
    for section in sections:
        name = str(section.get("name") or "").strip().casefold()
        if name:
            by_name.setdefault(name, []).append(section)

    matches = by_name.get(candidate.casefold()) or []
    match_kind = "exact_name"
    if not matches:
        # Layer 1: same term after normalization (plural/punctuation only).
        wanted = _normalize_section_term(candidate)
        matches = [s for s in sections if wanted and _normalize_section_term(s.get("name")) == wanted]
        match_kind = "normalized_name"
    if not matches:
        # Layer 2: same product-category concept in either language.
        concept = _section_concept(candidate)
        matches = [s for s in sections if concept and _section_concept(s.get("name")) == concept]
        match_kind = "category_concept"
    if not matches:
        # Layer 3: the compound supplier label names the section.
        runs = _token_runs(candidate)
        run_concepts = {_section_concept(run) for run in runs} - {""}
        matches = [
            s
            for s in sections
            if _normalize_section_term(s.get("name")) in runs
            or (_section_concept(s.get("name")) in run_concepts and _section_concept(s.get("name")))
        ]
        match_kind = "label_contains_section_name"
        if len(matches) > 1:
            deepest = _deepest_of_single_lineage(matches, sections)
            matches = [deepest] if deepest is not None else matches
    if not matches and evidence_concepts and not _names_a_product_category(candidate):
        # Layer 4: the supplier's category is an opaque internal code that
        # names no product category at all ("CE", "Consumer Electronics"),
        # so fall back to what the PREPARED product itself already says it
        # is -- its title and the canonical characteristic keys enrichment
        # resolved. Deliberately NOT applied when the supplier did name a
        # product category this catalog happens to lack (e.g.
        # "Холодильники" with no fridge section): that is a disagreement
        # between the supplier and the product, and it keeps failing
        # closed rather than silently overriding the supplier. Still only
        # ever selects an EXISTING section, and still fails closed below
        # unless exactly one survives.
        matches = _sections_for_concepts(evidence_concepts, sections)
        match_kind = "product_evidence_concept"
        if len(matches) > 1:
            deepest = _deepest_of_single_lineage(matches, sections)
            matches = [deepest] if deepest is not None else matches
    if len(matches) == 1:
        match = matches[0]
        return {
            "section_id": match.get("id"),
            "name": match.get("name"),
            "code": match.get("code"),
            "matched_on": candidate,
            "match_kind": match_kind,
        }
    if len(matches) > 1:
        raise SectionResolutionError(
            "ambiguous_section_name",
            f"{len(matches)} existing Bitrix sections match {candidate!r} "
            f"({', '.join(sorted(str(m.get('name')) for m in matches))}); refusing to guess which one",
        )
    raise SectionResolutionError(
        "no_matching_section_found",
        # The candidate AND the size of the snapshot it was matched
        # against are part of the message on purpose: a production
        # failure has to say WHICH value could not be resolved and
        # whether the section list Panda actually received was empty.
        f"no existing Bitrix section matched {candidate!r} "
        f"(checked {len(sections)} sections read from catalog.section.list)",
    )

PANDA_MANAGED = "PANDA_MANAGED"
BITRIX_MANAGED = "BITRIX_MANAGED"
ASPRO_MANAGED = "ASPRO_MANAGED"
DERIVED = "DERIVED"
READ_ONLY = "READ_ONLY"
UNMANAGED_PRESERVE = "UNMANAGED_PRESERVE"

OWNERSHIP_LABELS = frozenset(
    {PANDA_MANAGED, BITRIX_MANAGED, ASPRO_MANAGED, DERIVED, READ_ONLY, UNMANAGED_PRESERVE}
)

# The one property that models the offer -> parent product relationship.
CML2_LINK_PROPERTY_ID = 279
CML2_LINK_PROPERTY_CODE = "CML2_LINK"
# How catalog.product.offer.list actually exposes that same relationship.
CML2_LINK_REST_FIELD = "parentId"


@dataclass(frozen=True)
class PropertyBinding:
    property_id: int
    code: str
    ownership: str
    notes: str = ""

    @property
    def select_key(self) -> str:
        """The ``catalog.product(.offer).list`` ``select``/response key for
        this custom property (e.g. property id 100 -> ``property100``)."""
        return f"property{self.property_id}"


# IBLOCK 14 -- catalog product properties (spec section 3, "known real
# properties"). Any property NOT listed here is deliberately treated as
# UNMANAGED_PRESERVE by ``property_ownership`` below -- unknown data is
# never assumed writable.
CATALOG_PRODUCT_PROPERTIES: tuple[PropertyBinding, ...] = (
    PropertyBinding(97, "MINIMUM_PRICE", DERIVED, "Bitrix-computed rollup across offers; never written directly."),
    PropertyBinding(98, "MAXIMUM_PRICE", DERIVED, "Bitrix-computed rollup across offers; never written directly."),
    PropertyBinding(99, "HIT", BITRIX_MANAGED, "Merchandising flag curated by site editors."),
    PropertyBinding(100, "BRAND", PANDA_MANAGED, "Brand reference/enum property; canonical Product.brand maps here."),
    PropertyBinding(101, "IN_STOCK", DERIVED, "Bitrix-computed availability flag; never written directly."),
    PropertyBinding(102, "EXTENDED_REVIEWS_COUNT", READ_ONLY, "Review aggregation, read-only."),
    PropertyBinding(103, "EXTENDED_REVIEWS_RATING", READ_ONLY, "Review aggregation, read-only."),
    PropertyBinding(104, "STORES_FILTER", UNMANAGED_PRESERVE, "Store/availability filter metadata outside this boundary."),
    PropertyBinding(105, "LINK_REGION", UNMANAGED_PRESERVE, "Region-linking metadata outside this boundary."),
    PropertyBinding(106, "BNR_TOP_UNDER_HEADER", ASPRO_MANAGED, "Aspro Premier banner content."),
    PropertyBinding(107, "BNR_TOP", ASPRO_MANAGED, "Aspro Premier banner content."),
    PropertyBinding(108, "BNR_TOP_IMG", ASPRO_MANAGED, "Aspro Premier banner content."),
    PropertyBinding(109, "BNR_TOP_BG", ASPRO_MANAGED, "Aspro Premier banner content."),
    PropertyBinding(110, "BNR_TOP_COLOR", ASPRO_MANAGED, "Aspro Premier banner content."),
    PropertyBinding(111, "BUTTON1TEXT", ASPRO_MANAGED, "Aspro Premier banner button content."),
    PropertyBinding(112, "BUTTON1LINK", ASPRO_MANAGED, "Aspro Premier banner button content."),
    PropertyBinding(113, "BUTTON1TARGET", ASPRO_MANAGED, "Aspro Premier banner button content."),
    PropertyBinding(114, "BUTTON1CLASS", ASPRO_MANAGED, "Aspro Premier banner button content."),
    PropertyBinding(115, "BUTTON1COLOR", ASPRO_MANAGED, "Aspro Premier banner button content."),
    PropertyBinding(136, "LINK_TIZERS", ASPRO_MANAGED, "Aspro Premier teaser/banner links."),
    # Follow-up "complete product card" pass -- verified via
    # catalog.productProperty.list (module docstring item C), which
    # surfaces the real Russian admin-panel label; see
    # CATALOG_CHARACTERISTICS below for the Panda-facing semantic key each
    # one is exposed under.
    PropertyBinding(154, "PROP_2053", PANDA_MANAGED, "Screen diagonal, cm ('Диагональ дисплея, см'); see CATALOG_CHARACTERISTICS['screen_diagonal_cm']."),
    PropertyBinding(156, "PROP_2054", PANDA_MANAGED, "Screen resolution, px ('Разрешение экрана, пикс'); see CATALOG_CHARACTERISTICS['screen_resolution']."),
    PropertyBinding(206, "PROP_301", PANDA_MANAGED, "Operating system ('Операционная система'); see CATALOG_CHARACTERISTICS['operating_system']."),
    PropertyBinding(209, "PROP_304", PANDA_MANAGED, "Smart TV support ('Поддержка Smart TV'); see CATALOG_CHARACTERISTICS['smart_tv_support']."),
    PropertyBinding(246, "COLOR_REF2", PANDA_MANAGED, "Product color, directory-referenced ('Цвет'); see CATALOG_CHARACTERISTICS['color']."),
)

# IBLOCK 15 -- offers/SKU properties (spec section 4). property 279
# (CML2_LINK) is intentionally READ_ONLY here -- see CML2_LINK_* constants
# above; it is never written and is exposed via ``parentId``, not a
# ``property279`` select key, when using catalog.product.offer.list.
OFFER_PROPERTIES: tuple[PropertyBinding, ...] = (
    PropertyBinding(278, "STORES_FILTER", UNMANAGED_PRESERVE, "Store/availability filter metadata outside this boundary."),
    PropertyBinding(279, "CML2_LINK", READ_ONLY, "Parent-product identity link; see CML2_LINK_REST_FIELD ('parentId')."),
    PropertyBinding(280, "MORE_PHOTO", PANDA_MANAGED, "Offer gallery media; maps through the existing Artifact/media model."),
    PropertyBinding(281, "POPUP_VIDEO", UNMANAGED_PRESERVE, "Theme-specific popup video, outside this boundary."),
    PropertyBinding(282, "COLOR_REF", PANDA_MANAGED, "Variant attribute (color reference)."),
    PropertyBinding(283, "ARTICLE", PANDA_MANAGED, "Offer SKU/article identity."),
    PropertyBinding(284, "SIZES", PANDA_MANAGED, "Variant attribute (size)."),
    PropertyBinding(285, "VOLUME", PANDA_MANAGED, "Variant attribute (volume)."),
    PropertyBinding(286, "SIZES5", UNMANAGED_PRESERVE, "Category-specific sizing variant outside current scope."),
    PropertyBinding(287, "AGE", UNMANAGED_PRESERVE, "Category-specific attribute (apparel) outside current scope."),
    PropertyBinding(288, "TALL", UNMANAGED_PRESERVE, "Category-specific attribute (apparel) outside current scope."),
    PropertyBinding(289, "RUKAV", UNMANAGED_PRESERVE, "Category-specific attribute (apparel) outside current scope."),
    PropertyBinding(290, "FRELITE", UNMANAGED_PRESERVE, "Category-specific attribute outside current scope."),
    PropertyBinding(291, "FRLINE", UNMANAGED_PRESERVE, "Category-specific attribute outside current scope."),
    PropertyBinding(292, "FRCOLLECTION", UNMANAGED_PRESERVE, "Category-specific attribute outside current scope."),
    PropertyBinding(293, "FRTYPE", UNMANAGED_PRESERVE, "Category-specific attribute outside current scope."),
    PropertyBinding(294, "FRMADEIN", UNMANAGED_PRESERVE, "Category-specific attribute outside current scope."),
    PropertyBinding(295, "WEIGHT", PANDA_MANAGED, "Variant attribute (weight)."),
    PropertyBinding(296, "SIZES2", UNMANAGED_PRESERVE, "Category-specific sizing variant outside current scope."),
    PropertyBinding(297, "SIZES3", UNMANAGED_PRESERVE, "Category-specific sizing variant outside current scope."),
)

@dataclass(frozen=True)
class CharacteristicBinding:
    """A verified Panda-semantic-key -> IBLOCK 14 property binding for
    product characteristics/specifications (module docstring item C).

    ``key`` is the stable, Panda-facing semantic name (e.g.
    ``"screen_diagonal_cm"``) that a canonical product's ``characteristics``
    dict is keyed by; ``property_id``/``bitrix_code`` are this
    installation's real destination (cross-referenced in
    ``CATALOG_PRODUCT_PROPERTIES`` above); ``bitrix_name`` is the exact
    admin-panel label ``catalog.productProperty.list`` returned, kept here
    as the verification evidence for why this mapping is safe (not
    guessed). ``unit`` is only ever descriptive metadata -- this module
    never converts a value's unit; the caller is responsible for supplying
    it in whatever unit ``bitrix_name`` states.
    """

    key: str
    property_id: int
    bitrix_code: str
    bitrix_name: str
    unit: str = ""


# Only characteristics with an unambiguous, verified semantic match
# (module docstring item C) -- "display technology", "refresh rate" and
# "model/year" (all requested examples) have NO matching property on this
# installation and are deliberately absent; Panda may still carry that
# source data, it is simply never written to a guessed property.
CATALOG_CHARACTERISTICS: tuple[CharacteristicBinding, ...] = (
    CharacteristicBinding("screen_diagonal_cm", 154, "PROP_2053", "Диагональ дисплея, см", unit="cm"),
    CharacteristicBinding("screen_resolution", 156, "PROP_2054", "Разрешение экрана, пикс", unit="px (WxH)"),
    CharacteristicBinding("operating_system", 206, "PROP_301", "Операционная система"),
    CharacteristicBinding("smart_tv_support", 209, "PROP_304", "Поддержка Smart TV"),
    CharacteristicBinding("color", 246, "COLOR_REF2", "Цвет"),
)

_CHARACTERISTIC_BY_KEY = {c.key: c for c in CATALOG_CHARACTERISTICS}


def characteristic_binding(key: str) -> CharacteristicBinding | None:
    return _CHARACTERISTIC_BY_KEY.get(key)


def map_characteristics_to_properties(characteristics) -> tuple[dict, list[str]]:
    """Deterministically resolve a Panda ``{key: value}`` characteristics
    mapping to verified ``propertyN`` Bitrix write fields (module docstring
    item C). Returns ``(fields, unmapped_keys)``: ``fields`` only ever
    contains keys from ``CATALOG_CHARACTERISTICS`` above -- an unrecognized
    key is NEVER written to a guessed property, it is only reported back
    in ``unmapped_keys`` so a caller can report it as sourced-but-unwritten
    (Panda still preserves it, this function just refuses to invent a
    destination for it). Empty/``None`` values are skipped entirely
    (never written as an empty/zero value)."""
    fields: dict = {}
    unmapped: list[str] = []
    for key, value in dict(characteristics or {}).items():
        if value in (None, ""):
            continue
        binding = characteristic_binding(key)
        if binding is None:
            unmapped.append(key)
            continue
        fields[f"property{binding.property_id}"] = value
    return fields, unmapped


_CATALOG_BY_ID = {p.property_id: p for p in CATALOG_PRODUCT_PROPERTIES}
_CATALOG_BY_CODE = {p.code: p for p in CATALOG_PRODUCT_PROPERTIES}
_OFFER_BY_ID = {p.property_id: p for p in OFFER_PROPERTIES}
_OFFER_BY_CODE = {p.code: p for p in OFFER_PROPERTIES}


def catalog_property(*, property_id: int | None = None, code: str | None = None) -> PropertyBinding | None:
    if property_id is not None:
        return _CATALOG_BY_ID.get(property_id)
    if code is not None:
        return _CATALOG_BY_CODE.get(code)
    return None


def offer_property(*, property_id: int | None = None, code: str | None = None) -> PropertyBinding | None:
    if property_id is not None:
        return _OFFER_BY_ID.get(property_id)
    if code is not None:
        return _OFFER_BY_CODE.get(code)
    return None


def property_ownership(code: str, *, offer: bool = False) -> str:
    """Ownership for a known property; unknown properties are conservatively
    UNMANAGED_PRESERVE -- never assumed writable just because they appear on
    a live-read response (spec section 32: preserve unmanaged fields)."""
    binding = offer_property(code=code) if offer else catalog_property(code=code)
    return binding.ownership if binding else UNMANAGED_PRESERVE


def catalog_select_fields() -> list[str]:
    """Base + known-property ``select`` list for ``catalog.product.list`` /
    ``catalog.product.get`` reads of this installation's IBLOCK 14."""
    base = ["id", "iblockId", "name", "active", "code", "xmlId", "iblockSectionId", "quantity"]
    content = ["previewText", "previewPicture", "detailText", "detailPicture"]
    # Native physical fields (module docstring item D) -- explicit select
    # required, exactly like "id"/"iblockId"; never included by a bare "*".
    physical = [WEIGHT_FIELD, LENGTH_FIELD, WIDTH_FIELD, HEIGHT_FIELD]
    props = [p.select_key for p in CATALOG_PRODUCT_PROPERTIES]
    return base + content + physical + props


def offer_select_fields() -> list[str]:
    """Base + known-property ``select`` list for ``catalog.product.offer.list``
    reads of this installation's IBLOCK 15 (excludes CML2_LINK/279 -- that
    relationship is carried by the ``parentId`` field itself, not a
    ``propertyN`` select key)."""
    base = ["id", "iblockId", "name", "active", "code", "xmlId", "quantity", CML2_LINK_REST_FIELD]
    physical = [WEIGHT_FIELD, LENGTH_FIELD, WIDTH_FIELD, HEIGHT_FIELD]
    props = [p.select_key for p in OFFER_PROPERTIES if p.property_id != CML2_LINK_PROPERTY_ID]
    return base + physical + props


def map_catalog_product(item: dict) -> dict:
    """Decompose one raw ``catalog.product.list`` item into the ownership-
    aware sections required by spec section 8 (A/B/C/D/E/H/K), without
    fabricating anything the response did not actually contain."""
    props = {k[len("property"):]: v for k, v in item.items() if k.startswith("property") and k[8:].isdigit()}

    characteristics = []
    for pid_str, value in props.items():
        pid = int(pid_str)
        binding = catalog_property(property_id=pid)
        characteristics.append(
            {
                "property_id": pid,
                "code": binding.code if binding else "",
                "value": unwrap_property_value(value),
                "ownership": binding.ownership if binding else UNMANAGED_PRESERVE,
                "known": binding is not None,
            }
        )

    aspro_fields = {c["code"]: c["value"] for c in characteristics if c["ownership"] == ASPRO_MANAGED}
    brand = next((c["value"] for c in characteristics if c["code"] == "BRAND"), None)

    return {
        "identity": {
            "id": item.get("id"),
            "iblock_id": item.get("iblockId"),
            "name": item.get("name"),
            "active": item.get("active"),
            "xml_id": item.get("xmlId"),
            "code": item.get("code"),
        },
        "category": {
            # Only the product's OWN current section is known from this
            # response; the ancestor chain is resolved dynamically via
            # ``resolve_section_ancestors`` against a live/fixture
            # ``catalog.section.list`` read -- never assumed here.
            "section_id": item.get("iblockSectionId"),
        },
        "content": {
            "preview_text": item.get("previewText"),
            "preview_picture": item.get("previewPicture"),
            "detail_text": item.get("detailText"),
            "detail_picture": item.get("detailPicture"),
        },
        # Native physical fields (module docstring item D) -- present only
        # when the caller's ``select`` actually requested them (see
        # ``catalog_select_fields``); a key genuinely absent from the raw
        # response stays ``None`` here rather than becoming a fabricated 0.
        "physical": {
            "weight": item.get(WEIGHT_FIELD),
            "length": item.get(LENGTH_FIELD),
            "width": item.get(WIDTH_FIELD),
            "height": item.get(HEIGHT_FIELD),
        },
        "brand": {"value": brand, "ownership": PANDA_MANAGED if brand is not None else None},
        "characteristics": characteristics,
        "aspro": aspro_fields,
        "stock": {
            "total_quantity": item.get("quantity"),
            # Store/warehouse-specific inventory is a distinct concept
            # (catalog.storeProduct.*) that is NEVER inferred from this
            # total -- spec section 6/H.
            "warehouse_stock": None,
        },
    }


def resolve_section_ancestors(section_id: int | str, sections_by_id: dict) -> list[dict]:
    """Walk ``iblockSectionId`` (parent) links from a ``catalog.section.list``
    index to build the real ancestor chain for one section -- dynamic
    resolution, never a hardcoded "belongs to category 70" assumption
    (spec section 2/8-B)."""
    chain: list[dict] = []
    seen: set = set()
    current = sections_by_id.get(int(section_id)) if str(section_id).strip() else None
    while current and current.get("id") not in seen:
        seen.add(current.get("id"))
        chain.append({"id": current.get("id"), "name": current.get("name")})
        parent_id = current.get("iblockSectionId") or current.get("parentSectionId")
        current = sections_by_id.get(int(parent_id)) if parent_id else None
    return list(reversed(chain))


def map_offer(item: dict, *, parent_product_id: int | str | None = None) -> dict:
    """Decompose one raw ``catalog.product.offer.list`` item (spec section 8-F).

    Never assumes offer id == product id; the parent link comes from the
    REST ``parentId`` field (backed by CML2_LINK/279 in the underlying
    IBLOCK, per the module docstring), optionally cross-checked against an
    explicitly-passed ``parent_product_id`` from the query filter.
    """
    props = {k[len("property"):]: v for k, v in item.items() if k.startswith("property") and k[8:].isdigit()}
    variant_attrs = []
    for pid_str, value in props.items():
        pid = int(pid_str)
        binding = offer_property(property_id=pid)
        variant_attrs.append(
            {
                "property_id": pid,
                "code": binding.code if binding else "",
                "value": unwrap_property_value(value),
                "ownership": binding.ownership if binding else UNMANAGED_PRESERVE,
                "known": binding is not None,
            }
        )
    # LIVE discovery follow-up: ``parentId`` itself comes back wrapped in
    # the exact same ``{"value": ..., "valueId": ...}`` envelope as any
    # other custom property (it is backed by CML2_LINK/279 under the
    # hood) -- never a bare scalar on this installation. Unwrapping here
    # keeps ``matches_queried_parent`` comparing real ids instead of a
    # dict against a string (which could never match).
    parent_id = unwrap_property_value(item.get(CML2_LINK_REST_FIELD))
    return {
        "identity": {
            "id": item.get("id"),
            "iblock_id": item.get("iblockId"),
            "name": item.get("name"),
            "active": item.get("active"),
            "xml_id": item.get("xmlId"),
            "article": next((v["value"] for v in variant_attrs if v["code"] == "ARTICLE"), None),
        },
        "parent_link": {
            "property_id": CML2_LINK_PROPERTY_ID,
            "property_code": CML2_LINK_PROPERTY_CODE,
            "rest_field": CML2_LINK_REST_FIELD,
            "parent_product_id": parent_id,
            "matches_queried_parent": (
                str(parent_id) == str(parent_product_id) if parent_product_id is not None else None
            ),
        },
        "variant_attributes": variant_attrs,
        "stock": {"total_quantity": item.get("quantity"), "warehouse_stock": None},
    }


def map_prices(price_rows: list[dict], *, price_type_names: dict | None = None) -> list[dict]:
    """Group ``catalog.price.list`` rows -- one entry per (productId,
    catalogGroupId) pair is preserved distinctly; regional prices
    (e.g. MSC/EKB/MAGNITOGORSK) are never collapsed into a single canonical
    value (spec section 6/G)."""
    names = price_type_names or {}
    out = []
    for row in price_rows:
        group_id = row.get("catalogGroupId")
        out.append(
            {
                "price_id": row.get("id"),
                "product_id": row.get("productId"),
                "price_type_id": group_id,
                "price_type_name": names.get(group_id) or names.get(str(group_id)) or "",
                "amount": row.get("price"),
                "currency": row.get("currency"),
            }
        )
    return out


# Bitrix's ``catalog_product`` REST object (as returned by
# catalog.product.list) does not include an inherited/effective SEO field --
# only explicit per-element overrides could ever surface as an ordinary
# property, and none of the known real properties above represent one.
SEO_INHERITED_UNAVAILABLE_REASON = (
    "catalog.product.list (scope 'catalog') does not expose IPROPERTY "
    "inherited/effective SEO values; that merge is a server-side Bitrix "
    "computation, not a plain REST field. No additional scope is known to "
    "expose it either -- this is a REST-surface gap, not a permissions gap."
)


def seo_effective_status(item: dict) -> dict:
    """Classify SEO availability instead of fabricating an "effective"
    value (spec section 7/10): explicit overrides are reported if present
    on the response; the inherited/effective value is reported as
    unavailable with the exact reason, never guessed."""
    explicit_title = item.get("metaTitle") or item.get("seoTitle")
    explicit_description = item.get("metaDescription") or item.get("seoDescription")
    return {
        "explicit_seo_title": explicit_title,
        "explicit_seo_description": explicit_description,
        "effective_seo_available": False,
        "effective_seo_unavailable_reason": SEO_INHERITED_UNAVAILABLE_REASON,
    }
