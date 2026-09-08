"""Real Aspro Premier / Bitrix Field Mapping Matrix (spec section 8) and
per-field ownership classification (spec section 16).

This module is the mandatory acceptance artifact requested by the Block 5.6
specification: an explicit, testable mapping from every Panda concept
touched by this integration to its Bitrix/Aspro schema location, plus an
ownership tag for every field so a normal Panda write can never silently
touch a Bitrix/Aspro/derived-owned value.

CRITICAL CONSTRAINT (spec section 8): this connector MUST NOT invent
IBLOCK_ID / PROPERTY_ID / PROPERTY_CODE / PRICE_TYPE_ID / STORE_ID / Aspro
property semantics for the *specific* panda.msk.ru installation. Every
entry whose real value is installation-specific carries ``config_ref`` --
the name of a non-secret environment/config variable that must be populated
with the value discovered from the real installed schema (Bitrix admin
panel / REST introspection) before LIVE use. Until populated,
``schema_status`` truthfully reports ``PENDING_REAL_SCHEMA`` rather than a
guessed value -- this mirrors spec section 43's ``LIVE VERIFICATION
PENDING`` posture, applied to schema discovery instead of authentication.

Fields with genuinely standard, Bitrix-documented REST field codes (NAME,
ACTIVE, XML_ID, DETAIL_TEXT, PREVIEW_TEXT, DETAIL_PICTURE,
PREVIEW_PICTURE, CURRENCY, ...) are not installation-specific -- those are
literal Bitrix platform contract codes, not guessed for this store.

The observed price-type UI labels from the spec (OPT/MSC/EKB/MAGNITOGORSK)
are intentionally NOT hardcoded anywhere as real ``PRICE_TYPE_ID`` values --
spec section 19 explicitly warns "These NAMES are observations only."
``BITRIX_PRICE_TYPE_MAP`` is where the real per-installation ID for each
named price tier must be supplied.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# --- Ownership classification vocabulary (spec section 16, verbatim) ------

PANDA_MANAGED = "PANDA_MANAGED"
BITRIX_MANAGED = "BITRIX_MANAGED"
ASPRO_MANAGED = "ASPRO_MANAGED"
DERIVED = "DERIVED"
READ_ONLY = "READ_ONLY"
UNMANAGED_PRESERVE = "UNMANAGED_PRESERVE"

OWNERSHIP_LABELS = (PANDA_MANAGED, BITRIX_MANAGED, ASPRO_MANAGED, DERIVED, READ_ONLY, UNMANAGED_PRESERVE)

# Groups spec sections 14/15/31/32/33 require Panda to preserve untouched
# regardless of the real property codes behind them.
PRESERVE_ONLY_GROUPS = ("aspro_banner", "relations", "wildberries", "advertising")


@dataclass(frozen=True)
class FieldMappingEntry:
    panda_field: str
    bitrix_tab: str
    bitrix_entity: str
    bitrix_code: str
    config_ref: str
    bitrix_property_type: str
    multiple: bool
    aspro_purpose: str
    ownership: str
    notes: str

    def resolved_config_value(self) -> str:
        if not self.config_ref:
            return ""
        return str(os.environ.get(self.config_ref) or "").strip()

    @property
    def schema_status(self) -> str:
        if not self.config_ref:
            return "FIXED_BITRIX_FIELD"
        return "CONFIGURED" if self.resolved_config_value() else "PENDING_REAL_SCHEMA"


def _e(**kwargs) -> FieldMappingEntry:
    return FieldMappingEntry(**kwargs)


FIELD_MAPPING_MATRIX: tuple[FieldMappingEntry, ...] = (
    # --- Section 9 -- Tab "Элемент" -------------------------------------
    _e(panda_field="title", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="NAME",
       config_ref="", bitrix_property_type="string", multiple=False, aspro_purpose="Product name",
       ownership=PANDA_MANAGED, notes="Standard Bitrix REST field"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="ACTIVE",
       config_ref="", bitrix_property_type="boolean", multiple=False, aspro_purpose="Publication flag",
       ownership=PANDA_MANAGED, notes="Standard field; governed publish/deactivate op only"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="ACTIVE_FROM",
       config_ref="", bitrix_property_type="date", multiple=False, aspro_purpose="Activity start",
       ownership=BITRIX_MANAGED, notes="Not synchronized by Block 5.6"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="ACTIVE_TO",
       config_ref="", bitrix_property_type="date", multiple=False, aspro_purpose="Activity end",
       ownership=BITRIX_MANAGED, notes="Not synchronized by Block 5.6"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="CODE",
       config_ref="", bitrix_property_type="string", multiple=False, aspro_purpose="Symbolic code",
       ownership=BITRIX_MANAGED, notes="Bitrix-generated; Panda never overwrites"),
    _e(panda_field="article", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_ARTNUMBER_CODE", bitrix_property_type="string", multiple=False,
       aspro_purpose="Article/vendor code", ownership=PANDA_MANAGED,
       notes="Real PROPERTY_ID/CODE must come from installed schema; fixture default 'PROPERTY_ARTNUMBER' is FIXTURE-only"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="XML_ID",
       config_ref="", bitrix_property_type="string", multiple=False, aspro_purpose="External code / identity",
       ownership=PANDA_MANAGED, notes="Standard field; carries Panda product id for idempotent create"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="SORT",
       config_ref="", bitrix_property_type="number", multiple=False, aspro_purpose="Sort order",
       ownership=BITRIX_MANAGED, notes="Not synchronized by Block 5.6"),
    _e(panda_field="brand", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_BRAND_CODE", bitrix_property_type="element_binding", multiple=False,
       aspro_purpose="Brand reference (element binding, not plain text)", ownership=PANDA_MANAGED,
       notes="Real PROPERTY_ID/CODE + brand highload/iblock ref must come from installed schema"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_OFFER_LABEL_CODE", bitrix_property_type="string", multiple=False,
       aspro_purpose='"Наши предложения" sticker', ownership=BITRIX_MANAGED,
       notes="Preserve-only unless explicitly configured"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_STICKER_CODE", bitrix_property_type="string", multiple=True,
       aspro_purpose="Arbitrary sticker", ownership=BITRIX_MANAGED,
       notes="Preserve-only unless explicitly configured"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_PRODUCT_OF_DAY_CODE", bitrix_property_type="boolean", multiple=False,
       aspro_purpose="Product of the day", ownership=BITRIX_MANAGED,
       notes="Preserve-only unless explicitly configured"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_DISCONTINUED_CODE", bitrix_property_type="boolean", multiple=False,
       aspro_purpose="Discontinued flag", ownership=BITRIX_MANAGED,
       notes="Preserve-only unless explicitly configured"),
    _e(panda_field="", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_ANALOGUE_CODE", bitrix_property_type="element_binding", multiple=True,
       aspro_purpose="Analogue product link", ownership=BITRIX_MANAGED,
       notes="Preserve-only unless explicitly configured"),
    _e(panda_field="attributes", bitrix_tab="ЭЛЕМЕНТ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_CHARACTERISTICS_MAP", bitrix_property_type="string|list", multiple=True,
       aspro_purpose="Characteristics (Тип/Цвет/Размер/WiFi/Автоматический режим/...)", ownership=PANDA_MANAGED,
       notes="Real PROPERTY_ID/CODE per characteristic from a JSON map in this var, keyed by canonical "
             "attribute name -- never inferred from Russian labels"),

    # --- Section 10 -- Tab "Анонс" ---------------------------------------
    _e(panda_field="", bitrix_tab="АНОНС", bitrix_entity="product", bitrix_code="PREVIEW_PICTURE",
       config_ref="", bitrix_property_type="file", multiple=False, aspro_purpose="Preview/list image",
       ownership=BITRIX_MANAGED, notes="Distinct from DETAIL_PICTURE; confirm real template usage before treating as Panda-managed"),
    _e(panda_field="short_description", bitrix_tab="АНОНС", bitrix_entity="product", bitrix_code="PREVIEW_TEXT",
       config_ref="", bitrix_property_type="html", multiple=False, aspro_purpose="Short/preview description",
       ownership=PANDA_MANAGED, notes="Standard Bitrix field"),

    # --- Section 11 -- Tab "Подробно" -------------------------------------
    _e(panda_field="media_refs[0]", bitrix_tab="ПОДРОБНО", bitrix_entity="product", bitrix_code="DETAIL_PICTURE",
       config_ref="", bitrix_property_type="file", multiple=False, aspro_purpose="Main/detail image",
       ownership=PANDA_MANAGED, notes="Standard Bitrix field"),
    _e(panda_field="media_refs[1:]", bitrix_tab="ПОДРОБНО", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_GALLERY_PROPERTY_CODE", bitrix_property_type="file", multiple=True,
       aspro_purpose='"Картинки" gallery', ownership=PANDA_MANAGED,
       notes="Real PROPERTY_ID/CODE from installed schema -- do not assume equal to MORE_PHOTO without confirming"),
    _e(panda_field="", bitrix_tab="ПОДРОБНО", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PHOTOGALLERY_PROPERTY_CODE", bitrix_property_type="file", multiple=True,
       aspro_purpose='"Фотогалерея" (may be distinct from "Картинки")', ownership=BITRIX_MANAGED,
       notes="Only Panda-managed if the installed Aspro template actually consumes it for the primary gallery"),
    _e(panda_field="description", bitrix_tab="ПОДРОБНО", bitrix_entity="product", bitrix_code="DETAIL_TEXT",
       config_ref="", bitrix_property_type="html", multiple=False, aspro_purpose="Full/detailed description",
       ownership=PANDA_MANAGED, notes="Standard Bitrix field"),
    _e(panda_field="", bitrix_tab="ПОДРОБНО", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_PROPERTY_CAPTION_CODE", bitrix_property_type="string", multiple=False,
       aspro_purpose="Product caption", ownership=BITRIX_MANAGED, notes="Preserve-only unless explicitly configured"),

    # --- Section 13 -- Tab "Видео" -----------------------------------------
    _e(panda_field="", bitrix_tab="ВИДЕО", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_VIDEO_URL_PROPERTY_CODE", bitrix_property_type="string", multiple=False,
       aspro_purpose="Video in popup (URL)", ownership=BITRIX_MANAGED,
       notes="Distinct from image gallery semantics; preserve-only unless explicitly configured"),
    _e(panda_field="", bitrix_tab="ВИДЕО", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_VIDEO_IFRAME_PROPERTY_CODE", bitrix_property_type="html", multiple=False,
       aspro_purpose="Video/iframe embed code", ownership=BITRIX_MANAGED,
       notes="Preserve-only unless explicitly configured"),

    # --- Section 14 -- Tab "Баннер" (Aspro-specific, preserve-only) -------
    _e(panda_field="", bitrix_tab="БАННЕР", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_ASPRO_BANNER_PROPERTY_CODES", bitrix_property_type="mixed", multiple=True,
       aspro_purpose="Banner enabled/colors/images/button 1-2 class/URL/text/color/target",
       ownership=UNMANAGED_PRESERVE, notes="Aspro-only extension; never written by a normal Panda product update"),

    # --- Section 15 -- Tab "Связи" (preserve-only) ------------------------
    _e(panda_field="", bitrix_tab="СВЯЗИ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_RELATIONS_PROPERTY_CODES", bitrix_property_type="element_binding|section_binding",
       multiple=True, aspro_purpose="Region / Q&A / Articles / Promotions / Services",
       ownership=UNMANAGED_PRESERVE, notes="Never destroyed by a normal Panda product update"),

    # --- Section 16 -- Tab "Системные" (derived / read-only) --------------
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="CATALOG_AVAILABLE",
       config_ref="", bitrix_property_type="boolean", multiple=False, aspro_purpose="Availability",
       ownership=DERIVED, notes="Bitrix/catalog-computed"),
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_MIN_PRICE_PROPERTY_CODE", bitrix_property_type="number", multiple=False,
       aspro_purpose="Minimum price", ownership=DERIVED,
       notes="Bitrix-computed across price types/offers -- Panda never writes"),
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_MAX_PRICE_PROPERTY_CODE", bitrix_property_type="number", multiple=False,
       aspro_purpose="Maximum price", ownership=DERIVED,
       notes="Bitrix-computed across price types/offers -- Panda never writes"),
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="",
       config_ref="", bitrix_property_type="number", multiple=False, aspro_purpose="Review count / rating",
       ownership=DERIVED, notes="Bitrix/forum-computed -- Panda never writes"),
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="",
       config_ref="", bitrix_property_type="number", multiple=False, aspro_purpose="Comment count / vote count / rating sum",
       ownership=DERIVED, notes="Bitrix-computed -- Panda never writes"),
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="",
       config_ref="", bitrix_property_type="string", multiple=False, aspro_purpose="Requisites / tax rates",
       ownership=READ_ONLY, notes="Bitrix/1C-owned -- Panda never writes"),
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="",
       config_ref="", bitrix_property_type="string", multiple=False, aspro_purpose="Forum topic id",
       ownership=READ_ONLY, notes="Bitrix-owned"),
    _e(panda_field="", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="product", bitrix_code="MEASURE",
       config_ref="", bitrix_property_type="element_binding", multiple=False, aspro_purpose="Base unit",
       ownership=BITRIX_MANAGED, notes="Not synchronized by Block 5.6"),
    _e(panda_field="stock", bitrix_tab="СИСТЕМНЫЕ", bitrix_entity="store", bitrix_code="AMOUNT",
       config_ref="BITRIX_STORE_MAP", bitrix_property_type="number", multiple=True,
       aspro_purpose="Store/warehouse quantity", ownership=PANDA_MANAGED,
       notes="Real STORE_ID per named warehouse from a JSON map in this var; unknown stock never becomes 0; "
             "updating one warehouse never corrupts another"),

    # --- Section 17 -- Tab "SEO" -------------------------------------------
    _e(panda_field="seo_title", bitrix_tab="SEO", bitrix_entity="product",
       bitrix_code="IPROPERTY_TEMPLATES_ELEMENT_META_TITLE", config_ref="", bitrix_property_type="string",
       multiple=False, aspro_purpose="META TITLE", ownership=PANDA_MANAGED,
       notes="Standard Bitrix iproperty template field code"),
    _e(panda_field="seo_keywords", bitrix_tab="SEO", bitrix_entity="product",
       bitrix_code="IPROPERTY_TEMPLATES_ELEMENT_META_KEYWORDS", config_ref="", bitrix_property_type="string",
       multiple=False, aspro_purpose="META KEYWORDS", ownership=PANDA_MANAGED,
       notes="Populated only when canonical product carries seo_keywords"),
    _e(panda_field="seo_description", bitrix_tab="SEO", bitrix_entity="product",
       bitrix_code="IPROPERTY_TEMPLATES_ELEMENT_META_DESCRIPTION", config_ref="", bitrix_property_type="string",
       multiple=False, aspro_purpose="META DESCRIPTION", ownership=PANDA_MANAGED,
       notes="Standard Bitrix iproperty template field code"),
    _e(panda_field="seo_h1", bitrix_tab="SEO", bitrix_entity="product",
       bitrix_code="IPROPERTY_TEMPLATES_ELEMENT_TITLE", config_ref="", bitrix_property_type="string",
       multiple=False, aspro_purpose="Element title / H1", ownership=PANDA_MANAGED,
       notes="Populated only when canonical product carries seo_h1"),
    _e(panda_field="", bitrix_tab="SEO", bitrix_entity="product", bitrix_code="", config_ref="",
       bitrix_property_type="string", multiple=False, aspro_purpose="Preview/detail image ALT/TITLE",
       ownership=ASPRO_MANAGED, notes="Aspro template renders these from the iproperty fields above -- not a separate Panda field"),

    # --- Section 18 -- Tab "Разделы" ---------------------------------------
    _e(panda_field="category", bitrix_tab="РАЗДЕЛЫ", bitrix_entity="section", bitrix_code="IBLOCK_SECTION_ID",
       config_ref="BITRIX_PRODUCT_IBLOCK_ID", bitrix_property_type="element_binding", multiple=True,
       aspro_purpose="Section/category assignment", ownership=PANDA_MANAGED,
       notes="Bitrix section IDs are external references only -- never replace Panda's canonical category_id"),

    # --- Section 19 -- Tab "Торговый каталог" -------------------------------
    _e(panda_field="price.selling_price", bitrix_tab="ТОРГОВЫЙ КАТАЛОГ", bitrix_entity="price_type",
       bitrix_code="PRICE", config_ref="BITRIX_PRICE_TYPE_MAP", bitrix_property_type="number", multiple=True,
       aspro_purpose="Base/selling price per price type", ownership=PANDA_MANAGED,
       notes="Real PRICE_TYPE_ID per named tier from a JSON map in this var -- observed UI labels "
             "(OPT/MSC/EKB/MAGNITOGORSK) are NOT assumed real IDs. Updating one type never modifies another."),
    _e(panda_field="price.currency", bitrix_tab="ТОРГОВЫЙ КАТАЛОГ", bitrix_entity="price_type",
       bitrix_code="CURRENCY", config_ref="", bitrix_property_type="string", multiple=False,
       aspro_purpose="Currency", ownership=PANDA_MANAGED, notes="Standard Bitrix field"),
    _e(panda_field="", bitrix_tab="ТОРГОВЫЙ КАТАЛОГ", bitrix_entity="price_type", bitrix_code="",
       config_ref="BITRIX_VAT_INCLUDED_REF", bitrix_property_type="boolean", multiple=False,
       aspro_purpose="VAT included flag / VAT rate", ownership=BITRIX_MANAGED,
       notes="Preserve-only unless explicitly configured"),
    _e(panda_field="price.purchase_price", bitrix_tab="ТОРГОВЫЙ КАТАЛОГ", bitrix_entity="product",
       bitrix_code="PURCHASING_PRICE", config_ref="", bitrix_property_type="number", multiple=False,
       aspro_purpose="Purchase price", ownership=BITRIX_MANAGED, notes="Not written by Block 5.6 (Panda writes only selling price)"),

    # --- Section 21 -- Offers/SKU IBLOCK relationship -----------------------
    _e(panda_field="sku", bitrix_tab="ЭЛЕМЕНТ (offers)", bitrix_entity="offer", bitrix_code="",
       config_ref="BITRIX_OFFERS_IBLOCK_ID", bitrix_property_type="element_binding", multiple=False,
       aspro_purpose="Offer/SKU IBLOCK + parent product relationship (CML2_LINK)", ownership=PANDA_MANAGED,
       notes="Real offers IBLOCK_ID from installed schema -- fixture models offers as an in-memory dict keyed "
             "by offer_id, not a numeric IBLOCK"),

    # --- Section 32 -- Wildberries (preserve-only, NOT implemented) --------
    _e(panda_field="", bitrix_tab="WILDBERRIES", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_WB_PROPERTY_CODES", bitrix_property_type="mixed", multiple=True,
       aspro_purpose="WB sync status/error/nmId/chrtId/barcode", ownership=UNMANAGED_PRESERVE,
       notes="Block 5.7 scope -- Block 5.6 never reads or writes these"),

    # --- Section 33 -- Advertising / Yandex.Direct (preserve-only) ---------
    _e(panda_field="", bitrix_tab="РЕКЛАМА", bitrix_entity="product", bitrix_code="",
       config_ref="BITRIX_ADVERTISING_PROPERTY_CODES", bitrix_property_type="mixed", multiple=True,
       aspro_purpose="Yandex.Direct campaign metadata", ownership=UNMANAGED_PRESERVE,
       notes="Out of scope -- Block 5.6 never authorizes Yandex Passport or manages campaigns"),
)


REQUIRED_SCHEMA_CONFIG_VARS: tuple[str, ...] = tuple(sorted({e.config_ref for e in FIELD_MAPPING_MATRIX if e.config_ref}))


def matrix_report() -> list[dict]:
    """Bounded, secret-free summary for docs/PR reporting."""
    return [
        {
            "panda_field": e.panda_field,
            "tab": e.bitrix_tab,
            "entity": e.bitrix_entity,
            "bitrix_code": e.bitrix_code,
            "config_ref": e.config_ref,
            "type": e.bitrix_property_type,
            "multiple": e.multiple,
            "aspro_purpose": e.aspro_purpose,
            "ownership": e.ownership,
            "schema_status": e.schema_status,
            "notes": e.notes,
        }
        for e in FIELD_MAPPING_MATRIX
    ]


def pending_schema_vars() -> tuple[str, ...]:
    """Non-secret config var names still needing real installation values."""
    return tuple(
        sorted({e.config_ref for e in FIELD_MAPPING_MATRIX if e.config_ref and e.schema_status == "PENDING_REAL_SCHEMA"})
    )


def _root_field(panda_field: str) -> str:
    if not panda_field:
        return ""
    return panda_field.split(".")[0].split("[")[0]


# Canonical (product_intel) field -> ownership, derived from the matrix
# above (first occurrence wins; every occurrence of a given root field is
# kept consistent by construction).
CANONICAL_FIELD_OWNERSHIP: dict[str, str] = {}
for _entry in FIELD_MAPPING_MATRIX:
    _root = _root_field(_entry.panda_field)
    if _root:
        CANONICAL_FIELD_OWNERSHIP.setdefault(_root, _entry.ownership)

# Identity fields used only to resolve/address a target -- never a managed
# "write this value" field in their own right, so they pass through a
# write-payload sanitizer even though they are not independently listed as
# PANDA_MANAGED content fields.
IDENTITY_PASSTHROUGH_FIELDS = frozenset({"product_id", "sku", "article"})


def is_panda_writable(field: str) -> bool:
    return CANONICAL_FIELD_OWNERSHIP.get(field) == PANDA_MANAGED


def sanitize_canonical_for_write(product: dict) -> dict:
    """Defensive pre-mapping filter (Acceptance W).

    Drops any canonical Product Intelligence field that is not classified
    ``PANDA_MANAGED`` in the Field Mapping Matrix before it can reach the
    Bitrix payload mapping boundary (``integrations.bitrix.mapping``). This
    means a future field added to the canonical Product model can never
    silently reach Bitrix without an explicit ownership rule being added
    here first -- fail closed by omission, not by exception.
    """
    out: dict = {}
    for k, v in product.items():
        if k in IDENTITY_PASSTHROUGH_FIELDS:
            out[k] = v
        elif is_panda_writable(k):
            out[k] = v
    return out
