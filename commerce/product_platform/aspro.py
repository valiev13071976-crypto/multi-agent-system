"""Aspro Premier — configurable Bitrix storefront profile (not a separate core)."""

from __future__ import annotations

from commerce.product_platform.models import ASPRO_PROFILE_VERSION, AsproPremierProfile


def fixture_aspro_premier_profile() -> AsproPremierProfile:
    """Deterministic fixture — not universal Aspro field truth."""
    return AsproPremierProfile(
        profile_id="aspro_premier_fixture_v1",
        version=ASPRO_PROFILE_VERSION,
        field_mappings={
            "name": "NAME",
            "article": "PROPERTY_ARTNUMBER",
            "brand": "PROPERTY_BRAND",
            "description": "DETAIL_TEXT",
        },
        price_type_mapping={"RETAIL": "BASE", "PURCHASE": "PURCHASING"},
        stock_mapping={"main": "CATALOG_QUANTITY"},
        seo_mapping={"title": "IPROPERTY_TEMPLATES_ELEMENT_META_TITLE", "description": "IPROPERTY_TEMPLATES_ELEMENT_META_DESCRIPTION"},
        media_mapping={"primary": "DETAIL_PICTURE", "gallery": "MORE_PHOTO"},
        source_of_rules="configurable",
    )


def map_product_to_bitrix_payload(*, product: dict, profile: AsproPremierProfile) -> dict:
    """Translate canonical product fields via Aspro profile → Bitrix-shaped payload."""
    fm = profile.field_mappings
    return {
        fm.get("name", "NAME"): product.get("title") or product.get("name") or "",
        fm.get("article", "PROPERTY_ARTNUMBER"): product.get("sku") or "",
        fm.get("brand", "PROPERTY_BRAND"): product.get("brand") or "",
        fm.get("description", "DETAIL_TEXT"): product.get("description") or "",
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "integration": "bitrix",
        "storefront": "aspro_premier",
    }
