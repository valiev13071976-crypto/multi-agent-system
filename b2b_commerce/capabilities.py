"""Domain capabilities for B2B commerce."""

from __future__ import annotations

from autonomy.capabilities import CAP_TELEGRAM_READ, CAP_TELEGRAM_SEND

CAP_B2B_READ = "b2b.read"
CAP_B2B_SUPPLIER_READ = "b2b.supplier.read"
CAP_B2B_SUPPLIER_WRITE = "b2b.supplier.write"
CAP_B2B_WHOLESALE_READ = "b2b.wholesale.read"
CAP_B2B_WHOLESALE_INGEST = "b2b.wholesale.ingest"
CAP_B2B_WHOLESALE_COMPARE = "b2b.wholesale.compare"
CAP_B2B_CUSTOMER_READ = "b2b.customer.read"
CAP_B2B_CUSTOMER_WRITE = "b2b.customer.write"
CAP_B2B_QUOTE_READ = "b2b.quote.read"
CAP_B2B_QUOTE_CREATE = "b2b.quote.create"
CAP_B2B_QUOTE_APPROVE = "b2b.quote.approve"
CAP_B2B_QUOTE_SEND = "b2b.quote.send"
CAP_B2B_ORDER_READ = "b2b.order.read"
CAP_B2B_ORDER_DRAFT = "b2b.order.draft"
CAP_B2B_ORDER_SUBMIT = "b2b.order.submit"
CAP_B2B_ASSISTANT_USE = "b2b.assistant.use"
CAP_B2B_ASSISTANT_PROPOSE = "b2b.assistant.propose"
CAP_B2B_ASSISTANT_EXECUTE = "b2b.assistant.execute"

__all__ = [
    "CAP_B2B_READ",
    "CAP_B2B_SUPPLIER_READ",
    "CAP_B2B_SUPPLIER_WRITE",
    "CAP_B2B_WHOLESALE_READ",
    "CAP_B2B_WHOLESALE_INGEST",
    "CAP_B2B_WHOLESALE_COMPARE",
    "CAP_B2B_CUSTOMER_READ",
    "CAP_B2B_CUSTOMER_WRITE",
    "CAP_B2B_QUOTE_READ",
    "CAP_B2B_QUOTE_CREATE",
    "CAP_B2B_QUOTE_APPROVE",
    "CAP_B2B_QUOTE_SEND",
    "CAP_B2B_ORDER_READ",
    "CAP_B2B_ORDER_DRAFT",
    "CAP_B2B_ORDER_SUBMIT",
    "CAP_TELEGRAM_READ",
    "CAP_TELEGRAM_SEND",
    "CAP_B2B_ASSISTANT_USE",
    "CAP_B2B_ASSISTANT_PROPOSE",
    "CAP_B2B_ASSISTANT_EXECUTE",
]
