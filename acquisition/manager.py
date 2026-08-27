"""Acquisition Manager — orchestrates fetch via ToolGateway only."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from acquisition.errors import (
    AcquisitionDeniedError,
    AcquisitionTimeoutError,
    RateLimitedError,
    SourceUnavailableError,
)
from acquisition.models import (
    ACQ_BROWSER,
    ACQ_CRAWL,
    ACQ_DOCUMENT,
    ACQ_HTTP_GET,
    ACQ_SEARCH,
    CONTENT_TRUST_UNTRUSTED,
    AcquisitionRequest,
    RawArtifact,
    SourceDescriptor,
    checksum_bytes,
    checksum_text,
    new_id,
    utc_now,
)
from acquisition.registry import SourceRegistry
from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_FILESYSTEM_READ
from tools.models import ToolRequest


def _map_tool_error(error_code: str | None) -> Exception:
    code = str(error_code or "source_unavailable")
    if "timeout" in code:
        return AcquisitionTimeoutError()
    if "rate" in code:
        return RateLimitedError()
    if "permission" in code or "denied" in code or "policy" in code or "disabled" in code:
        return AcquisitionDeniedError(code if code.startswith("acquisition_") else "acquisition_denied")
    if "unavailable" in code or "not_found" in code:
        return SourceUnavailableError()
    return SourceUnavailableError(code if code.startswith(("source_", "acquisition_")) else "source_unavailable")


class AcquisitionManager:
    """Deterministic acquisition — all network via ToolGateway."""

    def __init__(
        self,
        *,
        source_registry: SourceRegistry,
        tool_gateway,
        store=None,
    ):
        self.sources = source_registry
        self.gateway = tool_gateway
        self.store = store

    def _require_source(self, request: AcquisitionRequest) -> SourceDescriptor:
        source = self.sources.get(request.source_id, tenant_id=request.tenant_id)
        if not source.enabled:
            raise AcquisitionDeniedError("source_disabled")
        return source

    async def acquire(self, request: AcquisitionRequest) -> RawArtifact:
        source = self._require_source(request)
        if request.acquisition_type == ACQ_HTTP_GET:
            return await self._acquire_http(request, source)
        if request.acquisition_type == ACQ_SEARCH:
            return await self._acquire_search(request, source)
        if request.acquisition_type == ACQ_DOCUMENT:
            return await self._acquire_document(request, source)
        if request.acquisition_type == ACQ_BROWSER:
            return await self._acquire_browser(request, source)
        if request.acquisition_type == ACQ_CRAWL:
            # Single-page seed fetch; full crawl uses Crawler
            return await self._acquire_http(request, source)
        raise AcquisitionDeniedError("unsupported_acquisition_type")

    async def _invoke(self, tool_id: str, operation: str, arguments: dict, request: AcquisitionRequest):
        caps = (CAP_EXTERNAL_READ, CAP_FILESYSTEM_READ)
        tool_req = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id=request.workflow_id or "acquisition",
            task_id="acquire",
            tool_id=tool_id,
            operation=operation,
            arguments=arguments,
            tenant_id=request.tenant_id,
            requested_capabilities=caps,
        )
        from autonomy.capabilities import CapabilitySet
        from autonomy.models import utc_now as a_now

        result = await self.gateway.invoke(
            tool_req,
            capabilities=CapabilitySet(
                subject_id="acquisition",
                capabilities=caps,
                issued_at=a_now(),
            ),
        )
        if not result.success:
            raise _map_tool_error(result.error_code)
        return result

    async def _acquire_http(self, request: AcquisitionRequest, source: SourceDescriptor) -> RawArtifact:
        url = str(request.target or "")
        # Domain policy from source
        if source.allowed_domains:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
            allowed = {d.lower() for d in source.allowed_domains}
            if host not in allowed and not any(host.endswith("." + d) for d in allowed):
                raise AcquisitionDeniedError("domain_not_allowed")
        result = await self._invoke(
            source.tool_id or "http.request",
            "get",
            {"url": url, "integration_id": source.integration_id},
            request,
        )
        data = dict(result.data or {})
        body = str(data.get("body_text") or "")
        checksum = checksum_text(body)
        artifact = RawArtifact(
            artifact_id=new_id("art-"),
            source_id=source.source_id,
            tenant_id=request.tenant_id,
            content_type=str(data.get("content_type") or "text/html"),
            fetched_at=utc_now(),
            checksum=checksum,
            content_text=body,
            content_bytes_len=len(body.encode("utf-8")),
            url=url,
            content_trust=CONTENT_TRUST_UNTRUSTED,
            provenance={
                "tool_id": source.tool_id or "http.request",
                "adapter_id": result.adapter_id or "http",
                "acquisition_id": request.request_id,
                "workflow_id": request.workflow_id,
                "source_trust": source.trust_level,
            },
            metadata={"acquisition_type": request.acquisition_type},
        )
        return self._persist_artifact(artifact)

    async def _acquire_search(self, request: AcquisitionRequest, source: SourceDescriptor) -> RawArtifact:
        query = str(request.target or "")
        result = await self._invoke(
            source.tool_id or "search",
            "search",
            {"query": query, "max_results": int(dict(request.constraints).get("max_results") or 5)},
            request,
        )
        data = dict(result.data or {})
        # Normalize search hits into JSON artifact
        results = data.get("results") or data.get("items") or []
        if not results and "title" not in data:
            # SearchReadAdapter may return nested structure — keep raw safe keys
            results = []
            for key in ("results", "hits", "items"):
                if isinstance(data.get(key), list):
                    results = data[key]
                    break
        payload = {"query": query, "results": results if isinstance(results, list) else [], "raw_keys": sorted(data.keys())[:32]}
        # Also try to lift SearchResult-like dicts from data itself
        if not payload["results"] and data:
            # FakeSearchProvider via invoke returns structured data from SearchReadAdapter
            maybe = data.get("value") if "value" in data else None
            _ = maybe
        body = json.dumps(payload, ensure_ascii=False, default=str)
        # Prefer gateway search() when available for richer results
        if hasattr(self.gateway, "search") and not payload["results"]:
            try:
                rows = await self.gateway.search(query, max_results=int(dict(request.constraints).get("max_results") or 5))
                payload["results"] = [
                    {
                        "title": r.title,
                        "url": r.url,
                        "snippet": r.snippet,
                        "source_domain": r.source_domain,
                        "rank": i + 1,
                    }
                    for i, r in enumerate(rows or [])
                ]
                body = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                pass
        checksum = checksum_text(body)
        artifact = RawArtifact(
            artifact_id=new_id("art-"),
            source_id=source.source_id,
            tenant_id=request.tenant_id,
            content_type="application/x-search-results",
            fetched_at=utc_now(),
            checksum=checksum,
            content_text=body,
            content_bytes_len=len(body.encode("utf-8")),
            content_trust=CONTENT_TRUST_UNTRUSTED,
            provenance={
                "tool_id": "search",
                "acquisition_id": request.request_id,
                "source_trust": source.trust_level,
            },
            metadata={"query": query, "record_hint": "search"},
        )
        return self._persist_artifact(artifact)

    async def _acquire_document(self, request: AcquisitionRequest, source: SourceDescriptor) -> RawArtifact:
        doc_id = str(request.target or "")
        result = await self._invoke(
            source.tool_id or "document.parse",
            "parse",
            {"document_id": doc_id},
            request,
        )
        data = dict(result.data or {})
        body = json.dumps(data, ensure_ascii=False, default=str)
        artifact = RawArtifact(
            artifact_id=new_id("art-"),
            source_id=source.source_id,
            tenant_id=request.tenant_id,
            content_type="application/x-document-ref",
            fetched_at=utc_now(),
            checksum=checksum_text(body),
            content_text=body,
            content_bytes_len=len(body.encode("utf-8")),
            document_id=doc_id,
            content_trust=CONTENT_TRUST_UNTRUSTED,
            provenance={
                "tool_id": "document.parse",
                "acquisition_id": request.request_id,
                "document_id": doc_id,
                "source_trust": source.trust_level,
            },
            metadata={"record_hint": "document"},
        )
        return self._persist_artifact(artifact)

    async def _acquire_browser(self, request: AcquisitionRequest, source: SourceDescriptor) -> RawArtifact:
        # Browser scaffold — explicit partial: try tool, map unavailable cleanly
        try:
            result = await self._invoke(
                source.tool_id or "browser.navigate",
                "fetch",
                {"url": str(request.target or "")},
                request,
            )
        except Exception as exc:
            raise SourceUnavailableError("browser_unavailable") from exc
        data = dict(result.data or {})
        body = str(data.get("body_text") or data.get("html") or "")
        artifact = RawArtifact(
            artifact_id=new_id("art-"),
            source_id=source.source_id,
            tenant_id=request.tenant_id,
            content_type="text/html",
            fetched_at=utc_now(),
            checksum=checksum_text(body),
            content_text=body,
            content_bytes_len=len(body.encode("utf-8")),
            url=str(request.target or ""),
            content_trust=CONTENT_TRUST_UNTRUSTED,
            provenance={
                "tool_id": "browser.navigate",
                "acquisition_id": request.request_id,
                "source_trust": source.trust_level,
                "browser": True,
            },
            metadata={"acquisition_type": ACQ_BROWSER},
        )
        return self._persist_artifact(artifact)

    def ingest_text_artifact(
        self,
        *,
        source: SourceDescriptor,
        tenant_id: str,
        text: str,
        content_type: str,
        url: str = "",
        metadata: dict | None = None,
        acquisition_id: str = "",
    ) -> RawArtifact:
        """Local/test path for already-fetched content (still goes through store/provenance)."""
        body = text or ""
        artifact = RawArtifact(
            artifact_id=new_id("art-"),
            source_id=source.source_id,
            tenant_id=tenant_id,
            content_type=content_type,
            fetched_at=utc_now(),
            checksum=checksum_text(body),
            content_text=body,
            content_bytes_len=len(body.encode("utf-8")),
            url=url,
            content_trust=CONTENT_TRUST_UNTRUSTED,
            provenance={
                "tool_id": "local_ingest",
                "acquisition_id": acquisition_id or new_id("acq-"),
                "source_trust": source.trust_level,
            },
            metadata=metadata or {},
        )
        return self._persist_artifact(artifact)

    def _persist_artifact(self, artifact: RawArtifact) -> RawArtifact:
        if self.store is None:
            return artifact
        existing = self.store.find_artifact_by_checksum(
            artifact.checksum, tenant_id=artifact.tenant_id, source_id=artifact.source_id
        )
        if existing is not None:
            return existing
        return self.store.save_artifact(artifact)
