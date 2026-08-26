"""Bounded supplier search adapter — ToolGateway path only; no arbitrary URLs."""

from __future__ import annotations

import re
from datetime import datetime

from memory.models import utc_now
from procurement.adapters.models import (
    OP_SEARCH,
    TOOL_SUPPLIER_SEARCH,
    SupplierSearchResult,
)
from procurement.errors import ProcurementError
from procurement.models import TRUST_EXTERNAL, TRUST_UNVERIFIED, content_hash_text
from security.redaction import redact
from tools.errors import ToolError, ToolTimeoutError
from tools.url_safety import UnsafeUrlError, is_safe_http_url, validate_http_url


PROCUREMENT_EXTERNAL_SEARCH_DISABLED = "procurement_external_search_disabled"
PROCUREMENT_EXTERNAL_QUERY_INVALID = "procurement_external_query_invalid"
PROCUREMENT_EXTERNAL_SOURCE_DENIED = "procurement_external_source_denied"
PROCUREMENT_EXTERNAL_TIMEOUT = "procurement_external_timeout"
PROCUREMENT_EXTERNAL_RATE_LIMITED = "procurement_external_rate_limited"

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
_SECRET_MARKERS = ("sk-", "ghp_", "Bearer ", "Authorization:", "PANDA_ENCRYPTION_KEY")


def _reject_forbidden_args(args: dict) -> None:
    for key in args:
        if str(key).lower() in _FORBIDDEN_ARG_KEYS:
            raise ProcurementError(PROCUREMENT_EXTERNAL_QUERY_INVALID, details={"field": key})


def _sanitize_text(text: str, *, max_len: int = 2000) -> str:
    cleaned = _CONTROL_RE.sub(" ", str(text or ""))
    cleaned = redact(cleaned)
    if any(m in cleaned for m in _SECRET_MARKERS):
        cleaned = "[REDACTED]"
    return cleaned[:max_len]


class SupplierSearchAdapter:
    """Read-only supplier search using injected search backend (Fake or gateway search)."""

    tool_id = TOOL_SUPPLIER_SEARCH
    operation = OP_SEARCH

    def __init__(
        self,
        *,
        backend=None,
        enabled: bool = True,
        max_results: int = 5,
    ):
        self.backend = backend
        self.enabled = bool(enabled)
        self.max_results = int(max_results)
        self.calls: list[dict] = []
        self.write_calls = 0

    async def execute_read(self, request, context) -> dict:
        _ = context
        if not self.enabled:
            raise ToolError(PROCUREMENT_EXTERNAL_SEARCH_DISABLED)
        args = dict(request.arguments or {})
        try:
            _reject_forbidden_args(args)
        except ProcurementError as exc:
            raise ToolError(exc.reason) from exc
        product_name = str(args.get("product_name") or "").strip()
        if not product_name or len(product_name) > 200:
            raise ToolError(PROCUREMENT_EXTERNAL_QUERY_INVALID)
        category = str(args.get("category") or "general").strip()[:80]
        country = str(args.get("country") or "").strip().upper()[:2] or None
        limit = min(int(args.get("limit") or self.max_results), self.max_results)
        required_specs = args.get("required_specs") if isinstance(args.get("required_specs"), dict) else {}
        self.calls.append(
            {
                "product_name_len": len(product_name),
                "category": category,
                "limit": limit,
            }
        )
        query = f"{category} supplier {product_name}".strip()
        try:
            rows = await self._backend_search(query, max_results=limit)
        except ToolError:
            raise
        except TimeoutError as exc:
            raise ToolTimeoutError() from exc
        stamp = utc_now()
        out = []
        for row in rows[:limit]:
            website = row.get("website_ref") or row.get("url")
            if website:
                try:
                    website = validate_http_url(str(website))
                except UnsafeUrlError as exc:
                    raise ToolError(PROCUREMENT_EXTERNAL_SOURCE_DENIED) from exc
                if not is_safe_http_url(website):
                    continue
            name = _sanitize_text(row.get("supplier_name") or row.get("title") or "Unknown", max_len=120)
            snippet = _sanitize_text(row.get("snippet_safe") or row.get("snippet") or "", max_len=500)
            if snippet == "[REDACTED]":
                continue
            supplier_ref = str(row.get("supplier_ref") or f"ext:{content_hash_text(name)[:12]}")
            trust = TRUST_EXTERNAL if website else TRUST_UNVERIFIED
            result = SupplierSearchResult(
                supplier_name=name,
                supplier_ref=supplier_ref,
                source="search_provider",
                retrieved_at=stamp,
                trust_level=trust,
                provenance={
                    "tool_id": self.tool_id,
                    "source": "search_provider",
                    "source_ref": supplier_ref,
                    "retrieved_at": stamp.isoformat(),
                    "content_hash": content_hash_text(f"{name}|{snippet}"),
                    "trust_level": trust,
                    "freshness": "on_demand",
                },
                categories=(category,),
                country=country,
                website_ref=website,
                snippet_safe=snippet,
                metadata_safe={"required_specs_keys": sorted(str(k) for k in required_specs.keys())[:20]},
            )
            out.append(result.as_dict())
        return {"suppliers": out, "count": len(out), "tool_id": self.tool_id}

    async def _backend_search(self, query: str, *, max_results: int) -> list[dict]:
        if self.backend is None:
            return []
        if getattr(self.backend, "status_code", None) == 429:
            raise ToolError(PROCUREMENT_EXTERNAL_RATE_LIMITED)
        if hasattr(self.backend, "search_suppliers"):
            return list(await self.backend.search_suppliers(query, max_results=max_results))
        if hasattr(self.backend, "search"):
            raw = self.backend.search(query, max_results=max_results)
            if hasattr(raw, "__await__"):
                raw = await raw
            mapped = []
            for item in list(raw or [])[:max_results]:
                if isinstance(item, dict):
                    mapped.append(item)
                else:
                    mapped.append(
                        {
                            "supplier_name": getattr(item, "title", "Supplier"),
                            "snippet": getattr(item, "snippet", ""),
                            "url": getattr(item, "url", None),
                            "supplier_ref": f"web:{getattr(item, 'source_domain', 'unknown')}",
                        }
                    )
            return mapped
        return []


class FakeSupplierSearchBackend:
    """Deterministic offline backend for tests/evals."""

    def __init__(self, results_by_query: dict[str, list[dict]] | None = None):
        self.results_by_query = results_by_query or {}
        self.queries: list[str] = []
        self.error: Exception | None = None
        self.status_code: int | None = None
        self.delay_seconds = 0.0

    async def search_suppliers(self, query: str, *, max_results: int = 5) -> list[dict]:
        self.queries.append(query)
        if self.status_code == 429:
            raise ToolError(PROCUREMENT_EXTERNAL_RATE_LIMITED)
        if self.error is not None:
            raise self.error
        q = query.casefold()
        matched: list[dict] = []
        for key, rows in self.results_by_query.items():
            if key.casefold() in q or q in key.casefold():
                matched.extend(rows)
        return matched[:max_results]
