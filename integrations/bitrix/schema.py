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
"""

from __future__ import annotations

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
    props = [p.select_key for p in CATALOG_PRODUCT_PROPERTIES]
    return base + content + props


def offer_select_fields() -> list[str]:
    """Base + known-property ``select`` list for ``catalog.product.offer.list``
    reads of this installation's IBLOCK 15 (excludes CML2_LINK/279 -- that
    relationship is carried by the ``parentId`` field itself, not a
    ``propertyN`` select key)."""
    base = ["id", "iblockId", "name", "active", "code", "xmlId", "quantity", CML2_LINK_REST_FIELD]
    props = [p.select_key for p in OFFER_PROPERTIES if p.property_id != CML2_LINK_PROPERTY_ID]
    return base + props


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
