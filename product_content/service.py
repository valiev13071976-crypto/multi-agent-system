"""Product Content Pipeline service — Blocks 12–14 orchestrator. Offline, no publish."""

from __future__ import annotations

from product_content.card import assemble_product_card
from product_content.category_policy import CategoryPolicy, resolve_category_policy
from product_content.contracts import ProductContentPackage
from product_content.media_package import ingest_media_assets
from product_content.package import assemble_package
from product_content.seo_package import build_seo_package
from product_content.store import ProductContentStore
from security.tenant import require_tenant_id


class ProductContentService:
    """ONE pipeline: identity → card → SEO → media → package → persist. No marketplace/CMS write."""

    def __init__(self, store: ProductContentStore | None = None) -> None:
        self.store = store or ProductContentStore()

    def build_package(
        self,
        row: dict,
        *,
        tenant_id: str,
        media: list[dict] | None = None,
        category_policy: CategoryPolicy | None = None,
        persist: bool = True,
    ) -> ProductContentPackage:
        tenant = require_tenant_id(tenant_id)
        policy = resolve_category_policy(row.get("category"), override=category_policy)
        card = assemble_product_card(row, tenant_id=tenant, policy=policy)
        occupied_slugs = self.store.occupied_slugs(tenant_id=tenant, exclude_product_id=card.product_id)
        occupied_titles = self.store.occupied_titles(tenant_id=tenant, exclude_product_id=card.product_id)
        media_pkg = ingest_media_assets(tenant_id=tenant, card=card, assets=list(media or []))
        image_refs = [a.asset_id for a in media_pkg.assets if a.validation_status == "VALID" and a.role == "MAIN"]
        seo = build_seo_package(
            card,
            policy=policy,
            occupied_slugs=occupied_slugs,
            occupied_titles=occupied_titles,
            image_refs=image_refs,
            extra_copy=str(row.get("marketing_copy") or ""),
        )
        sku_collisions = self.store.other_product_ids_for_sku(
            tenant_id=tenant, sku=card.sku, exclude_product_id=card.product_id
        )
        package = assemble_package(
            tenant_id=tenant,
            card=card,
            seo=seo,
            media=media_pkg,
            source_row=row,
            extra_warnings=tuple(f"duplicate_sku:{pid}" for pid in sku_collisions),
        )
        existing = self.store.get_by_version(tenant_id=tenant, version=package.version)
        if existing is not None:
            return existing
        if persist:
            self.store.save(package)
        return package

    def get_package(self, package_id: str, *, tenant_id: str) -> ProductContentPackage:
        return self.store.get(package_id, tenant_id=tenant_id)
