"""Product content pipeline errors — fail closed."""

from __future__ import annotations


class ProductContentError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.reason = code
        super().__init__(message or code)


CONTENT_ACCESS_DENIED = "CONTENT_ACCESS_DENIED"
CONTENT_PACKAGE_NOT_FOUND = "CONTENT_PACKAGE_NOT_FOUND"
CONTENT_UNSUPPORTED_CLAIM = "CONTENT_UNSUPPORTED_CLAIM"
CONTENT_TENANT_REQUIRED = "CONTENT_TENANT_REQUIRED"
CONTENT_IDENTITY_REQUIRED = "CONTENT_IDENTITY_REQUIRED"
