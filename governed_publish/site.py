"""Site/CMS payload mapping. Reuses Bitrix/Aspro mapping; does not invent price/stock."""

from __future__ import annotations

from commerce.product_platform.aspro import fixture_aspro_premier_profile, map_product_to_bitrix_payload
from integrations.bitrix.mapping import canonical_to_bitrix_payload
from product_content.contracts import ProductContentPackage, ROLE_MAIN

from governed_publish.contracts import content_hash


def _valid_media(package: ProductContentPackage) -> list[dict]:
    out = []
    for a in sorted(package.media.assets, key=lambda x: x.sort_order):
        if a.validation_status != "VALID":
            continue
        out.append(
            {
                "asset_id": a.asset_id,
                "role": a.role,
                "sort_order": a.sort_order,
                "kind": a.kind,
                "checksum": a.checksum,
                "alt_text": a.alt_text,
            }
        )
    return out


def canonical_site_fields(package: ProductContentPackage) -> dict:
    card = package.card
    specs = {k: v.normalized for k, v in card.specifications.items() if v.normalized}
    fields = {
        "external_id": card.product_id,
        "sku": card.sku,
        "article": card.article or card.sku,
        "name": card.canonical_title or card.product_name,
        "category": card.category,
        "brand": card.brand,
        "short_description": card.short_description,
        "long_description": card.long_description,
        "properties": specs,
        "seo_title": package.seo.seo_title,
        "meta_description": package.seo.meta_description,
        "slug": package.seo.canonical_slug,
        "media": _valid_media(package),
        "publication_state": "DRAFT_FIXTURE",
        "content_version": package.version,
        "tenant_id": package.tenant_id,
    }
    # Price/stock/discount: only if explicitly present — never invent
    if card.selling_price:
        fields["listing_price_preview_only"] = card.selling_price
    return fields


def to_bitrix_aspro_payload(package: ProductContentPackage, *, aspro: bool = True) -> dict:
    fields = canonical_site_fields(package)
    product = {
        "title": fields["name"],
        "name": fields["name"],
        "sku": fields["sku"],
        "article": fields["article"],
        "brand": fields["brand"],
        "description": fields["long_description"] or fields["short_description"],
    }
    if aspro:
        mapped = map_product_to_bitrix_payload(product=product, profile=fixture_aspro_premier_profile())
    else:
        mapped = canonical_to_bitrix_payload(product=product, aspro_enabled=False)
    mapped["SEO_TITLE"] = fields["seo_title"]
    mapped["SEO_DESCRIPTION"] = fields["meta_description"]
    mapped["CODE"] = fields["slug"]
    mapped["XML_ID"] = fields["external_id"]
    mapped["media_refs"] = [m["asset_id"] for m in fields["media"]]
    mapped["content_version"] = package.version
    mapped["mode"] = "FIXTURE"
    mapped["aspro_boundary"] = "offline_payload_only"
    return mapped


def snapshot_version(snapshot: dict | None) -> str:
    if not snapshot:
        return "none"
    return content_hash(dict(snapshot))
