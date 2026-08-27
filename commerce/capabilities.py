"""Domain capabilities for commerce — separate from LLM default grants."""

from __future__ import annotations

CAP_CATALOG_READ = "catalog.read"
CAP_CATALOG_WRITE = "catalog.write"
CAP_INVENTORY_READ = "inventory.read"
CAP_INVENTORY_RESERVE = "inventory.reserve"
CAP_INVENTORY_ADJUST = "inventory.adjust"
CAP_PRICING_PROPOSE = "pricing.propose"
CAP_PRICING_WRITE = "pricing.write"
CAP_ORDER_READ = "order.read"
CAP_ORDER_WRITE = "order.write"
CAP_EDO_READ = "edo.read"
CAP_EDO_PREPARE = "edo.prepare"
CAP_EDO_SEND = "edo.send"
CAP_MARKING_READ = "marking.read"
CAP_MARKING_TRANSFER = "marking.transfer"
CAP_MARKING_WITHDRAW = "marking.withdraw"
CAP_FISCAL_READ = "fiscal.read"
CAP_FISCAL_CREATE = "fiscal.create"
CAP_FISCAL_REFUND = "fiscal.refund"
CAP_SUPPLIER_READ = "supplier.read"
CAP_SUPPLIER_WRITE = "supplier.write"
CAP_COMMERCE_RECONCILE = "commerce.reconcile"

COMMERCE_CAPABILITIES = (
    CAP_CATALOG_READ,
    CAP_CATALOG_WRITE,
    CAP_INVENTORY_READ,
    CAP_INVENTORY_RESERVE,
    CAP_INVENTORY_ADJUST,
    CAP_PRICING_PROPOSE,
    CAP_PRICING_WRITE,
    CAP_ORDER_READ,
    CAP_ORDER_WRITE,
    CAP_EDO_READ,
    CAP_EDO_PREPARE,
    CAP_EDO_SEND,
    CAP_MARKING_READ,
    CAP_MARKING_TRANSFER,
    CAP_MARKING_WITHDRAW,
    CAP_FISCAL_READ,
    CAP_FISCAL_CREATE,
    CAP_FISCAL_REFUND,
    CAP_SUPPLIER_READ,
    CAP_SUPPLIER_WRITE,
    CAP_COMMERCE_RECONCILE,
)

# Default LLM deny — require explicit capability + policy + often HITL
LLM_DEFAULT_DENY = frozenset(
    {
        CAP_MARKING_WITHDRAW,
        CAP_FISCAL_REFUND,
        CAP_INVENTORY_ADJUST,
    }
)
