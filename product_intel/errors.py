"""Product Intelligence error taxonomy (Block 5.5).

Mirrors the existing ``ContentIntelError``/``DataIntelError`` shape (a typed
``code`` attribute, no parallel error protocol) per spec section 26: "Follow
existing normalized result/error contracts ... rather than inventing a
parallel error protocol."
"""

from __future__ import annotations


class ProductIntelError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.reason = code
        super().__init__(message or code)


PRODUCT_INVALID_INPUT = "INVALID_INPUT"
PRODUCT_AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
PRODUCT_AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
PRODUCT_INVALID_PRODUCT = "INVALID_PRODUCT"
PRODUCT_CONFLICT = "CONFLICT"
PRODUCT_CAPABILITY_DENIED = "CAPABILITY_DENIED"
PRODUCT_TENANT_VIOLATION = "TENANT_VIOLATION"
PRODUCT_ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
PRODUCT_SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
PRODUCT_EXECUTION_FAILED = "EXECUTION_FAILED"
PRODUCT_BATCH_REQUIRED = "PRODUCT_BATCH_REQUIRED"
PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"


class ProductBatchRequired(ProductIntelError):
    def __init__(self):
        super().__init__(PRODUCT_BATCH_REQUIRED)


class ProductCrossTenantError(ProductIntelError):
    def __init__(self):
        super().__init__(PRODUCT_TENANT_VIOLATION)
