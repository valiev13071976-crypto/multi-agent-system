"""Document tools via ToolGateway — wraps DocumentService."""

from __future__ import annotations

from tools.errors import ToolArgumentInvalidError, ToolNotFoundError


class DocumentToolAdapter:
    adapter_id = "document"

    def __init__(self, document_service=None):
        self._svc = document_service

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("document.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._svc is not None else ADAPTER_UNAVAILABLE

    async def execute_read(self, request, context) -> dict:
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        from memory.models import MemoryScope

        args = dict(request.arguments or {})
        tenant = str(request.tenant_id or "legacy-default")
        scope = MemoryScope(
            scope_type=str(args.get("scope_type") or "workspace"),
            scope_id=str(args.get("scope_id") or tenant),
            tenant_ref=tenant,
        )
        op = request.operation
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
