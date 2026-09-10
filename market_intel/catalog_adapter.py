"""Thin READ-ONLY adapter over the existing normalized product output.

This is the whole boundary between Market Intelligence and the existing
Excel / file-ingestion / supplier-price-list pipeline. That pipeline
already produces ``product_intel.platform_models.Product`` records and
already owns a deterministic matcher
(``product_intel.matching.match_against_catalog`` ->
``data_intel.product_match`` -> ``acquisition.entity``).

Market Intelligence therefore parses no spreadsheets, ingests no files,
normalizes no supplier rows and writes nothing back. It only:

  * reads existing ``Product`` records for a tenant, and
  * translates its own ``ExtractedOffer`` into the field names that
    existing matcher already understands.

Any future mismatch between the two contracts is absorbed HERE, on the
new side, never by changing the catalog contract.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from product_intel.matching import match_against_catalog
from product_intel.platform_models import MATCH_STATE_EXACT, MATCH_STATE_HIGH_CONFIDENCE

from market_intel.models import ExtractedOffer

# Match states we are willing to attribute an observed price to. Anything
# softer stays recorded but unattributed rather than polluting a product's
# price history.
ATTRIBUTABLE_STATES = frozenset({MATCH_STATE_EXACT, MATCH_STATE_HIGH_CONFIDENCE})


class CatalogSnapshot(Protocol):
    """The read side of the existing ``product_intel.store.ProductCatalogStore``."""

    def list_products(self, *, tenant_id: str, catalog_id: str | None = None) -> list:
        ...


def load_catalog(catalog: CatalogSnapshot | None, *, tenant_id: str, catalog_id: str = "") -> list:
    if catalog is None:
        return []
    return list(catalog.list_products(tenant_id=tenant_id, catalog_id=catalog_id or None))


def offer_candidate_fields(offer: ExtractedOffer) -> dict:
    """Translate an observed post into the matcher's own field vocabulary.

    ``model`` is offered as both ``mpn`` and ``sku`` because a supplier
    post never says which one it printed; the matcher decides, and a
    genuine hard-identifier conflict still blocks the match.
    """
    return {
        "product_id": "",
        "name": offer.title,
        "product_name": offer.title,
        "brand": offer.brand,
        "model": offer.model,
        "mpn": offer.model,
        "sku": offer.model,
        "ean": offer.ean,
    }


def match_offer(offer: ExtractedOffer, products: list):
    """Delegate to the EXISTING deterministic matcher — no second engine."""
    return match_against_catalog(offer_candidate_fields(offer), products)


def attributed_product_id(outcome) -> str:
    if outcome.state in ATTRIBUTABLE_STATES:
        return str(outcome.matched_product_id or "")
    return ""


def _product_price(product: Any):
    return getattr(product, "price", None)


def catalog_selling_price(product: Any) -> Decimal | None:
    price = _product_price(product)
    return getattr(price, "selling_price", None) if price is not None else None


def catalog_purchase_price(product: Any) -> Decimal | None:
    price = _product_price(product)
    return getattr(price, "purchase_price", None) if price is not None else None


def catalog_currency(product: Any) -> str:
    price = _product_price(product)
    return str(getattr(price, "currency", "") or "") if price is not None else ""


def find_product(products: list, product_id: str) -> Any | None:
    for product in products:
        if str(getattr(product, "product_id", "")) == str(product_id):
            return product
    return None
