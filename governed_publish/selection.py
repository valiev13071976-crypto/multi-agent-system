"""Selective export — wraps marketplace.selection; empty/ambiguous never means export-all."""

from __future__ import annotations

from marketplace.errors import MARKETPLACE_SELECTION_REQUIRED, MarketplaceError
from marketplace.selection import new_selection, require_explicit_selection, resolve_selection
from product_content.contracts import ProductContentPackage, STATUS_BLOCKED, STATUS_READY, STATUS_READY_WITH_WARNINGS, STATUS_REQUIRES_REVIEW

from governed_publish.contracts import SelectionResult
from governed_publish.errors import PUBLISH_EMPTY_SELECTION, GovernedPublishError


def packages_to_catalog(packages: list[ProductContentPackage]) -> list[dict]:
    rows = []
    for p in packages:
        rows.append(
            {
                "product_id": p.product_id,
                "sku_id": p.card.sku,
                "article": p.card.article or p.card.sku,
                "category_id": p.card.category,
                "brand": p.card.brand,
                "title": p.card.canonical_title or p.card.product_name,
                "stock": None,
            }
        )
    return rows


def select_packages(
    packages: list[ProductContentPackage],
    *,
    tenant_id: str,
    product_ids: tuple[str, ...] = (),
    skus: tuple[str, ...] = (),
    articles: tuple[str, ...] = (),
    categories: tuple[str, ...] = (),
    exclude_ids: tuple[str, ...] = (),
) -> SelectionResult:
    if not (product_ids or skus or articles or categories):
        raise GovernedPublishError(PUBLISH_EMPTY_SELECTION, "empty_selection_no_export")
    sku_ids = tuple(dict.fromkeys(list(skus) + list(articles)))
    selection = new_selection(
        tenant_id=tenant_id,
        product_ids=product_ids,
        sku_ids=sku_ids,
        category_ids=categories,
        allow_all_catalog=False,
    )
    try:
        require_explicit_selection(selection)
    except MarketplaceError as exc:
        raise GovernedPublishError(PUBLISH_EMPTY_SELECTION, getattr(exc, "code", str(exc))) from exc
    catalog = packages_to_catalog(packages)
    resolved = resolve_selection(selection=selection, catalog=catalog)
    by_id = {p.product_id: p for p in packages}
    selected_ids = [str(i["product_id"]) for i in resolved["selected"]]
    if articles:
        extra = [p.product_id for p in packages if (p.card.article or p.card.sku) in set(articles)]
        for pid in extra:
            if pid not in selected_ids:
                selected_ids.append(pid)
    excluded_sel = {str(i["product_id"]) for i in resolved["excluded"]}
    selected_ids = [pid for pid in selected_ids if pid not in set(exclude_ids)]
    blocked, ready, review = [], [], []
    for pid in selected_ids:
        pkg = by_id.get(pid)
        if pkg is None:
            continue
        if pkg.status == STATUS_BLOCKED:
            blocked.append(pid)
        elif pkg.status in {STATUS_REQUIRES_REVIEW, STATUS_READY_WITH_WARNINGS}:
            review.append(pid)
        elif pkg.status == STATUS_READY:
            ready.append(pid)
        else:
            review.append(pid)
    inspectable = {
        "selected_count": len(selected_ids),
        "excluded_count": len(excluded_sel | set(exclude_ids)),
        "blocked_count": len(blocked),
        "ready_count": len(ready),
        "review_required_count": len(review),
        "allow_all_catalog": False,
        "mode": resolved.get("mode"),
    }
    return SelectionResult(
        selected=tuple(selected_ids),
        excluded=tuple(sorted(excluded_sel | set(exclude_ids))),
        blocked=tuple(blocked),
        ready=tuple(ready),
        review=tuple(review),
        count=len(selected_ids),
        mode="EXPLICIT",
        inspectable=inspectable,
    )
