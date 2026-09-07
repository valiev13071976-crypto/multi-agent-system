"""Deterministic product validation layer (spec section 15).

Returns structured, machine-readable ``ProductValidationResult`` records
(VALID/WARNING/INVALID) instead of booleans, mirroring the existing
``DataIssue``/``CatalogQualityIssue`` shape used elsewhere in the repository.
"""

from __future__ import annotations

from collections import defaultdict

from product_intel.normalize import validate_ean
from product_intel.platform_models import (
    VALIDATION_INVALID,
    VALIDATION_VALID,
    VALIDATION_WARNING,
    Product,
    ProductValidationIssue,
    ProductValidationResult,
)

ISSUE_MISSING_IDENTITY = "MISSING_IDENTITY"
ISSUE_INVALID_BARCODE = "INVALID_BARCODE"
ISSUE_INVALID_PRICE = "INVALID_PRICE"
ISSUE_MISSING_PRICE = "MISSING_PRICE"
ISSUE_NEGATIVE_STOCK = "NEGATIVE_STOCK"
ISSUE_DUPLICATE_SKU = "DUPLICATE_SKU"
ISSUE_DUPLICATE_BARCODE = "DUPLICATE_BARCODE"
ISSUE_CONFLICTING_BARCODE_FOR_SKU = "CONFLICTING_BARCODE_FOR_SKU"
ISSUE_UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
ISSUE_CROSS_TENANT_REFERENCE = "CROSS_TENANT_REFERENCE"


def _issue(code: str, severity: str, message: str, field: str = "") -> ProductValidationIssue:
    return ProductValidationIssue(code=code, severity=severity, message=message, field=field)


def validate_product(product: Product) -> ProductValidationResult:
    issues: list[ProductValidationIssue] = []

    has_identity = bool(product.title) or bool(product.sku) or bool(product.article) or bool(product.gtin)
    if not has_identity:
        issues.append(_issue(ISSUE_MISSING_IDENTITY, "error", "Product has no title, SKU, article, or barcode.", "identity"))

    if product.gtin and not validate_ean(product.gtin):
        issues.append(_issue(ISSUE_INVALID_BARCODE, "error", f"Barcode '{product.gtin}' fails GS1 checksum.", "gtin"))

    price = product.price
    if price.selling_price is not None and price.selling_price < 0:
        issues.append(_issue(ISSUE_INVALID_PRICE, "error", "Selling price is negative.", "price.selling_price"))
    elif price.purchase_price is not None and price.purchase_price < 0:
        issues.append(_issue(ISSUE_INVALID_PRICE, "error", "Purchase price is negative.", "price.purchase_price"))
    elif price.selling_price is None and price.purchase_price is None:
        issues.append(_issue(ISSUE_MISSING_PRICE, "warning", "No price information supplied.", "price"))

    if price.currency and not (len(price.currency) == 3 and price.currency.isalpha()):
        issues.append(_issue(ISSUE_UNSUPPORTED_CURRENCY, "warning", f"Unsupported currency code '{price.currency}'.", "price.currency"))

    stock = product.stock
    if stock.quantity is not None and stock.quantity < 0:
        issues.append(_issue(ISSUE_NEGATIVE_STOCK, "error", "Stock quantity is negative.", "stock.quantity"))

    if product.source is not None and product.source.raw_snapshot:
        src_tenant = str(product.source.raw_snapshot.get("tenant_id") or "")
        if src_tenant and src_tenant != product.tenant_id:
            issues.append(
                _issue(
                    ISSUE_CROSS_TENANT_REFERENCE,
                    "error",
                    "Source reference belongs to a different tenant.",
                    "source",
                )
            )

    if any(i.severity == "error" for i in issues):
        state = VALIDATION_INVALID
    elif issues:
        state = VALIDATION_WARNING
    else:
        state = VALIDATION_VALID

    return ProductValidationResult(product_id=product.product_id, tenant_id=product.tenant_id, state=state, issues=tuple(issues))


def validate_catalog(products: list[Product]) -> dict[str, ProductValidationResult]:
    """Per-record validation plus cross-record duplicate/conflict checks.

    Returns a ``product_id -> ProductValidationResult`` map covering every
    product in ``products`` (never partial), each combining its own
    single-record issues with any catalog-level duplicate/conflict issues.
    """
    per_product: dict[str, list[ProductValidationIssue]] = {}
    base_state: dict[str, str] = {}
    for p in products:
        single = validate_product(p)
        per_product[p.product_id] = list(single.issues)
        base_state[p.product_id] = single.state

    by_sku: dict[str, list[str]] = defaultdict(list)
    by_barcode: dict[str, list[str]] = defaultdict(list)
    sku_to_barcodes: dict[str, set[str]] = defaultdict(set)
    for p in products:
        if p.sku:
            by_sku[p.sku].append(p.product_id)
            if p.gtin:
                sku_to_barcodes[p.sku].add(p.gtin)
        if p.gtin:
            by_barcode[p.gtin].append(p.product_id)

    for sku, ids in by_sku.items():
        if len(ids) > 1:
            for pid in ids:
                per_product[pid].append(
                    _issue(ISSUE_DUPLICATE_SKU, "error", f"SKU '{sku}' is used by {len(ids)} products.", "sku")
                )

    for barcode, ids in by_barcode.items():
        if len(ids) > 1:
            for pid in ids:
                per_product[pid].append(
                    _issue(ISSUE_DUPLICATE_BARCODE, "error", f"Barcode '{barcode}' is used by {len(ids)} products.", "gtin")
                )

    for sku, barcodes in sku_to_barcodes.items():
        if len(barcodes) > 1:
            for pid in by_sku[sku]:
                per_product[pid].append(
                    _issue(
                        ISSUE_CONFLICTING_BARCODE_FOR_SKU,
                        "warning",
                        f"SKU '{sku}' is associated with {len(barcodes)} different barcodes.",
                        "gtin",
                    )
                )

    results: dict[str, ProductValidationResult] = {}
    for p in products:
        issues = tuple(per_product[p.product_id])
        if any(i.severity == "error" for i in issues):
            state = VALIDATION_INVALID
        elif issues:
            state = VALIDATION_WARNING
        else:
            state = VALIDATION_VALID
        results[p.product_id] = ProductValidationResult(
            product_id=p.product_id, tenant_id=p.tenant_id, state=state, issues=issues
        )
    return results
