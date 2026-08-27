"""Document tools via ToolGateway — wraps DocumentService + DocumentIntelligence."""

from __future__ import annotations

import base64

from documents.errors import DocumentError
from tools.errors import ToolArgumentInvalidError, ToolNotFoundError, ToolError


class DocumentToolAdapter:
    adapter_id = "document"

    def __init__(self, document_service=None, intelligence=None):
        self._svc = document_service
        self._intel = intelligence

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("document.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._svc is not None or self._intel is not None else ADAPTER_UNAVAILABLE

    def _scope(self, request, args):
        from memory.models import MemoryScope

        tenant = str(request.tenant_id or "legacy-default")
        return MemoryScope(
            scope_type=str(args.get("scope_type") or "workspace"),
            scope_id=str(args.get("scope_id") or tenant),
            tenant_ref=tenant,
        ), tenant

    async def execute_read(self, request, context) -> dict:
        args = dict(request.arguments or {})
        op = request.operation
        scope, tenant = self._scope(request, args)

        # Intelligence-backed ops
        if op == "detect":
            if self._intel is None:
                raise ToolNotFoundError("tool_unavailable")
            filename = str(args.get("filename") or "file.bin")
            raw = args.get("content_b64") or args.get("content")
            if raw is None:
                raise ToolArgumentInvalidError()
            if isinstance(raw, str) and args.get("content_b64"):
                data = base64.b64decode(raw)
            elif isinstance(raw, (bytes, bytearray)):
                data = bytes(raw)
            else:
                data = str(raw).encode("utf-8")
            try:
                dtype, media = self._intel.detect_type(
                    filename=filename, data=data, media_type=args.get("media_type")
                )
            except DocumentError as exc:
                raise ToolError(exc.reason) from exc
            return {
                "document_type": dtype,
                "media_type": media,
                "filename": filename,
                "provenance": {"source": "document_intelligence", "op": "detect"},
            }

        if op == "ocr":
            if self._intel is None:
                raise ToolNotFoundError("tool_unavailable")
            raw = args.get("content_b64")
            if not raw:
                raise ToolArgumentInvalidError()
            data = base64.b64decode(raw)
            doc_id = str(args.get("document_id") or "ocr-ephemeral")
            try:
                content = self._intel.ocr_document(
                    doc_id,
                    tenant_id=tenant,
                    data=data,
                    filename=str(args.get("filename") or ""),
                )
            except DocumentError as exc:
                raise ToolError(exc.reason) from exc
            return {
                "document_id": content.document_id,
                "text_preview": (content.text or "")[:2000],
                "confidence": content.confidence,
                "extraction_method": content.extraction_method,
                "warnings": list(content.warnings),
                "provenance": {"source": "document_intelligence", "op": "ocr"},
            }

        if op == "structured_extract":
            if self._intel is None:
                raise ToolNotFoundError("tool_unavailable")
            text = str(args.get("text") or "")
            doc_id = str(args.get("document_id") or "structured-ephemeral")
            if not text and doc_id and self._svc is not None:
                try:
                    content = self._intel.extract_content(doc_id, tenant_id=tenant, scope=scope)
                    text = content.text
                except DocumentError as exc:
                    raise ToolError(exc.reason) from exc
            from documents.intelligence.contracts import DocumentContent

            content = DocumentContent(document_id=doc_id, text=text)
            try:
                structured = self._intel.structured_extract(
                    doc_id,
                    tenant_id=tenant,
                    content=content,
                    document_type=args.get("document_type"),
                    filename=str(args.get("filename") or ""),
                )
            except DocumentError as exc:
                raise ToolError(exc.reason) from exc
            return {
                "document_id": structured.document_id,
                "document_type": structured.document_type,
                "schema_version": structured.schema_version,
                "fields": dict(structured.fields),
                "identifiers": dict(structured.identifiers),
                "amounts": dict(structured.amounts),
                "dates": dict(structured.dates),
                "line_item_count": len(structured.line_items),
                "confidence": structured.confidence,
                "validation_ok": structured.validation_ok,
                "validation_errors": list(structured.validation_errors),
                "provenance": dict(structured.provenance),
            }

        if op == "compare":
            if self._intel is None:
                raise ToolNotFoundError("tool_unavailable")
            left_text = str(args.get("left_text") or "")
            right_text = str(args.get("right_text") or "")
            left_type = args.get("left_document_type")
            right_type = args.get("right_document_type")
            from documents.intelligence.contracts import DocumentContent

            left_c = DocumentContent(document_id="left", text=left_text)
            right_c = DocumentContent(document_id="right", text=right_text)
            left_s = self._intel.structured_extract(
                "left", tenant_id=tenant, content=left_c, document_type=left_type
            )
            right_s = self._intel.structured_extract(
                "right", tenant_id=tenant, content=right_c, document_type=right_type
            )
            result = self._intel.compare(left_s, right_s)
            return {
                "left_ref": result.left_ref,
                "right_ref": result.right_ref,
                "changed_fields": list(result.changed_fields),
                "added_sections": list(result.added_sections),
                "removed_sections": list(result.removed_sections),
                "table_differences": list(result.table_differences),
                "unchanged": result.unchanged,
                "summary": dict(result.summary),
                "provenance": {"source": "document_intelligence", "op": "compare"},
            }

        if op == "generate":
            if self._intel is None:
                raise ToolNotFoundError("tool_unavailable")
            fmt = str(args.get("format") or "txt")
            title = str(args.get("title") or "document")
            paragraphs = list(args.get("paragraphs") or [])
            try:
                gen = self._intel.generate(
                    tenant_id=tenant,
                    format=fmt,
                    title=title,
                    paragraphs=paragraphs,
                    tables=args.get("tables"),
                    headings=args.get("headings"),
                )
            except DocumentError as exc:
                raise ToolError(exc.reason) from exc
            return {
                "document_id": gen.document_id,
                "tenant_id": gen.tenant_id,
                "filename": gen.filename,
                "media_type": gen.media_type,
                "checksum": gen.checksum,
                "size": len(gen.content),
                "content_b64": base64.b64encode(gen.content).decode("ascii"),
                "template_id": gen.template_id,
                "provenance": dict(gen.provenance),
            }

        if op == "convert":
            if self._intel is None:
                raise ToolNotFoundError("tool_unavailable")
            try:
                gen = self._intel.convert(
                    tenant_id=tenant,
                    source_media_type=str(args.get("source_media_type") or "text/plain"),
                    target_format=str(args.get("target_format") or "pdf"),
                    text=str(args.get("text") or ""),
                    title=str(args.get("title") or "converted"),
                )
            except DocumentError as exc:
                raise ToolError(exc.reason) from exc
            return {
                "document_id": gen.document_id,
                "tenant_id": gen.tenant_id,
                "filename": gen.filename,
                "media_type": gen.media_type,
                "checksum": gen.checksum,
                "size": len(gen.content),
                "content_b64": base64.b64encode(gen.content).decode("ascii"),
                "provenance": dict(gen.provenance),
            }

        if op == "extract" and self._intel is not None and args.get("text") is not None:
            # Ephemeral text extract without store
            from documents.intelligence.contracts import DocumentContent

            content = DocumentContent(
                document_id=str(args.get("document_id") or "ephemeral"),
                text=str(args.get("text") or ""),
            )
            return {
                "document_id": content.document_id,
                "text_preview": content.text[:2000],
                "extraction_method": "direct",
                "provenance": {"source": "document_intelligence", "op": "extract"},
            }

        # Legacy DocumentService ops
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        if op == "search":
            from documents.models import DocumentSearchRequest

            q = str(args.get("query") or "")
            results = self._svc.search(
                DocumentSearchRequest(scope=scope, query=q, limit=int(args.get("limit") or 10)),
                requesting_scope=scope,
            )
            return {
                "results": [
                    {
                        "document_id": r.document_id,
                        "chunk_id": r.chunk_id,
                        "score": r.score,
                        "snippet": r.snippet_safe,
                        "citation_ref": r.citation_ref,
                    }
                    for r in results
                ],
                "provenance": {"source": "document_service"},
            }
        if op in {"parse", "extract", "list_chunks"}:
            doc_id = str(args.get("document_id") or "")
            if not doc_id:
                raise ToolArgumentInvalidError()
            row = self._svc.get(document_id=doc_id, requesting_scope=scope)
            if row is None:
                raise ToolNotFoundError()
            if op == "list_chunks":
                chunks = self._svc.list_chunks(doc_id, requesting_scope=scope)
                return {
                    "document_id": doc_id,
                    "chunks": [
                        {
                            "chunk_id": c.chunk_id,
                            "ordinal": c.ordinal,
                            "source_location": c.source_location,
                        }
                        for c in chunks
                    ],
                }
            return {
                "document_id": doc_id,
                "document_type": row.document_type,
                "status": row.status,
                "chunk_count": row.chunk_count,
                "provenance": {"source": "document_service"},
            }
        raise ToolArgumentInvalidError()
