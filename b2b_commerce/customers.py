"""B2B customer helpers."""

from __future__ import annotations

import uuid

from b2b_commerce.errors import B2B_CUSTOMER_NOT_FOUND, B2B_CUSTOMER_UNVERIFIED, B2BCommerceError
from b2b_commerce.platform_models import CUSTOMER_UNVERIFIED, B2BCustomer


def new_customer_id() -> str:
    return f"cust_{uuid.uuid4().hex[:12]}"


def require_customer(customer: B2BCustomer | None) -> B2BCustomer:
    if customer is None or customer.deleted:
        raise B2BCommerceError(B2B_CUSTOMER_NOT_FOUND)
    return customer


def require_verified_customer(customer: B2BCustomer, *, for_sensitive: bool = False) -> None:
    if for_sensitive and customer.verification_state == CUSTOMER_UNVERIFIED:
        raise B2BCommerceError(B2B_CUSTOMER_UNVERIFIED)
