"""Bounded catalog read — only registered/validated supplier refs."""

from __future__ import annotations

import re

from memory.models import utc_now
from procurement.adapters.models import (
    OP_CATALOG_READ,
    TOOL_CATALOG_READ,
    CatalogItem,
    SupplierCatalogResult,
)
from procurement.errors import ProcurementError
from procurement.models import TRUST_EXTERNAL, content_hash_text
from security.redaction import redact
from tools.errors import ToolError
from tools.url_safety import UnsafeUrlError, validate_http_url


PROCUREMENT_CATALOG_REF_INVALID = "procurement_catalog_ref_invalid"
PROCUREMENT_CATALOG_FETCH_FAILED = "procurement_catalog_fetch_failed"
PROCUREMENT_EXTERNAL_SOURCE_DENIED = "procurement_external_source_denied"

_FORBIDDEN_ARG_KEYS = frozenset(
    {
        "base_url",
        "headers",
        "auth",
        "authorization",
        "timeout",
        "method",
        "raw_url",
        "url",
        "shell",
        "command",
        "endpoint_url",
    }
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _reject_forbidden_args(args: dict) -> None:
    for key in args:
        if str(key).lower() in _FORBIDDEN_ARG_KEYS:
            raise ProcurementError(PROCUREMENT_CATALOG_REF_INVALID, details={"field": key})


class SupplierCatalogReadAdapter:
    """Read catalog for known supplier_ref / catalog_ref only — no arbitrary URLs."""

    tool_id = TOOL_CATALOG_READ
    operation = OP_CATALOG_READ

    def __init__(
        self,
        *,
        backend=None,
        allowed_refs: frozenset[str] | set[str] | None = None,
        max_items: int = 50,
        enabled: bool = True,
    ):
        self.backend = backend
        self.allowed_refs = set(allowed_refs or ())
        self.max_items = int(max_items)
        self.enabled = bool(enabled)
        self.calls: list[dict] = []
        self.write_calls = 0

    def allow_ref(self, ref: str) -> None:
        self.allowed_refs.add(str(ref))

    async def execute_read(self, request, context) -> dict:
        _ = context
        if not self.enabled:
            raise ToolError(PROCUREMENT_CATALOG_FETCH_FAILED)
        args = dict(request.arguments or {})
        try:
            _reject_forbidden_args(args)
        except ProcurementError as exc:
            raise ToolError(exc.reason) from exc
        supplier_ref = str(args.get("supplier_ref") or "").strip()
        catalog_ref = str(args.get("catalog_ref") or "").strip()
        if not supplier_ref or not catalog_ref:
            raise ToolError(PROCUREMENT_CATALOG_REF_INVALID)
        # Deny raw URL as catalog_ref unless explicitly allowlisted AND SSRF-safe
        if catalog_ref.lower().startswith(("http://", "https://", "file://")):
            try:
                validate_http_url(catalog_ref)
            except UnsafeUrlError as exc:
                raise ToolError(PROCUREMENT_EXTERNAL_SOURCE_DENIED) from exc
            if catalog_ref not in self.allowed_refs:
                raise ToolError(PROCUREMENT_CATALOG_REF_INVALID)
        if supplier_ref not in self.allowed_refs and catalog_ref not in self.allowed_refs:
            raise ToolError(PROCUREMENT_CATALOG_REF_INVALID)
        limit = min(int(args.get("limit") or self.max_items), self.max_items)
        self.calls.append({"supplier_ref": supplier_ref[:64], "limit": limit})
        try:
            items_raw = await self._fetch(supplier_ref, catalog_ref, limit=limit)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(PROCUREMENT_CATALOG_FETCH_FAILED) from exc
        stamp = utc_now()
        items = []
        for row in items_raw[:limit]:
            name = redact(_CONTROL_RE.sub(" ", str(row.get("name") or "")))[:200]
            if not name or name == "[REDACTED]":
                continue
            items.append(
                CatalogItem(
                    sku=str(row["sku"])[:64] if row.get("sku") is not None else None,
                    name=name,
                    unit_price=str(row["unit_price"]) if row.get("unit_price") is not None else None,
                    currency=str(row["currency"]).upper() if row.get("currency") else None,
                    quantity_available=str(row["quantity_available"])
                    if row.get("quantity_available") is not None
                    else None,
                    specifications=row.get("specifications")
                    if isinstance(row.get("specifications"), dict)
                    else {},
                )
            )
        result = SupplierCatalogResult(
            supplier_ref=supplier_ref,
            source_ref=catalog_ref,
            items=tuple(items),
            retrieved_at=stamp,
            freshness="on_demand",
            provenance={
                "tool_id": self.tool_id,
                "source": "registered_catalog",
                "source_ref": catalog_ref,
                "retrieved_at": stamp.isoformat(),
                "content_hash": content_hash_text(
                    "|".join(f"{i.name}:{i.unit_price}" for i in items)
                ),
                "trust_level": TRUST_EXTERNAL,
                "freshness": "on_demand",
            },
            currency=items[0].currency if items else None,
            availability="unknown",
            warnings=() if items else ("empty_catalog",),
        )
        return result.as_dict()

    async def _fetch(self, supplier_ref: str, catalog_ref: str, *, limit: int) -> list[dict]:
        if self.backend is None:
            return []
        if hasattr(self.backend, "read_catalog"):
            return list(
                await self.backend.read_catalog(supplier_ref, catalog_ref, max_items=limit)
            )
        return []


class FakeCatalogBackend:
    def __init__(self, catalogs: dict[str, list[dict]] | None = None):
        self.catalogs = catalogs or {}
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None

    async def read_catalog(self, supplier_ref: str, catalog_ref: str, *, max_items: int = 50):
        self.calls.append((supplier_ref, catalog_ref))
        if self.error is not None:
            raise self.error
        key = catalog_ref if catalog_ref in self.catalogs else supplier_ref
        return list(self.catalogs.get(key, []))[:max_items]
