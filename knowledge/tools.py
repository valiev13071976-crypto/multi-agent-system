"""Tool Platform adapter for Knowledge / Memory (Block 8)."""

from __future__ import annotations

from knowledge.errors import KnowledgeBatchRequired, KnowledgeError
from knowledge.models import KnowledgeIngestRequest, KnowledgeQuery, TRUST_OPERATOR
from knowledge.planner import assert_sync_ingest_allowed
from memory.models import MEMORY_SEMANTIC, MemoryIngestRequest, MemoryScope, SCOPE_PROJECT
from memory.write_decision import MemoryWriteRequest
from security.encryption import SENSITIVITY_INTERNAL
from tools.errors import ToolArgumentInvalidError, ToolNotFoundError


class KnowledgeToolAdapter:
    adapter_id = "knowledge"

    def __init__(self, knowledge_service=None, memory_service=None):
        self._knowledge = knowledge_service
        self._memory = memory_service

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("knowledge.") or tool_id.startswith("memory.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._knowledge is not None else ADAPTER_UNAVAILABLE

    def _scope(self, request) -> MemoryScope:
        tenant = str(request.tenant_id or "legacy-default")
        sid = str((request.arguments or {}).get("scope_id") or tenant)
        return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid, tenant_ref=tenant)

    async def execute_read(self, request, context) -> dict:
        if self._knowledge is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        scope = self._scope(request)
        op = request.operation
        if op == "retrieve":
            rows = self._knowledge.retrieve(
                KnowledgeQuery(
                    query_text=str(args.get("query") or ""),
                    scope=scope,
                    limit=int(args.get("limit") or 10),
                ),
                requesting_scope=scope,
            )
            return {
                "count": len(rows),
                "results": [
                    {
                        "knowledge_id": r.knowledge_id,
                        "citation_ref": r.citation_ref,
                        "score": r.score,
                        "source_id": r.source_id,
                    }
                    for r in rows
                ],
            }
        if op == "status":
            return {"enabled": bool(getattr(self._knowledge, "enabled", False))}
        if op == "read" and self._memory is not None:
            from memory.models import MemoryQuery

            mem_scope = self._scope(request)
            rows = self._memory.retrieve(
                MemoryQuery(query_text=str(args.get("query") or ""), scope=mem_scope),
                requesting_scope=mem_scope,
            )
            return {"count": len(rows)}
        raise ToolNotFoundError("operation_not_supported")

    async def execute_write(self, request, context) -> dict:
        args = dict(request.arguments or {})
        op = request.operation
        scope = self._scope(request)
        if op == "ingest":
            if self._knowledge is None:
                raise ToolNotFoundError("tool_unavailable")
            content = str(args.get("content") or "")
            if not content:
                raise ToolArgumentInvalidError()
            try:
                assert_sync_ingest_allowed(byte_size=len(content.encode("utf-8")))
            except KnowledgeBatchRequired as exc:
                raise ToolArgumentInvalidError(str(exc.code)) from exc
            source_id = str(args.get("source_id") or "manual.default")
            item = self._knowledge.ingest(
                KnowledgeIngestRequest(
                    scope=scope,
                    source_id=source_id,
                    content=content,
                    trust_level=str(args.get("trust_level") or TRUST_OPERATOR),
                    provenance_source_ref=str(args.get("provenance_ref") or source_id),
                    sensitivity=SENSITIVITY_INTERNAL,
                    validated=True,
                ),
                requesting_scope=scope,
            )
            return {"knowledge_id": item.knowledge_id, "citation_ref": item.citation_ref}
        if op == "delete":
            if self._knowledge is None:
                raise ToolNotFoundError("tool_unavailable")
            kid = str(args.get("knowledge_id") or "")
            if not kid:
                raise ToolArgumentInvalidError()
            return self._knowledge.delete_knowledge(
                knowledge_id=kid,
                requesting_scope=scope,
            )
        if op == "write":
            if self._memory is None:
                raise ToolNotFoundError("tool_unavailable")
            content = str(args.get("content") or "")
            if not content:
                raise ToolArgumentInvalidError()
            req = MemoryWriteRequest(
                scope=scope,
                ingest=MemoryIngestRequest(
                    scope=scope,
                    memory_type=str(args.get("memory_type") or MEMORY_SEMANTIC),
                    content=content,
                    source_type=str(args.get("source_type") or "user_input"),
                    source_id=str(args.get("source_id") or "user"),
                ),
                explicit_user_authorized=bool(args.get("explicit_user_authorized")),
                model_suggestion=bool(args.get("model_suggestion")),
                retrieved_content=bool(args.get("retrieved_content")),
            )
            record = self._memory.write_with_decision(req, requesting_scope=scope)
            return {"memory_id": record.memory_id}
        if op == "propose":
            if self._memory is None:
                raise ToolNotFoundError("tool_unavailable")
            content = str(args.get("content") or "")
            req = MemoryWriteRequest(
                scope=scope,
                ingest=MemoryIngestRequest(
                    scope=scope,
                    memory_type=MEMORY_SEMANTIC,
                    content=content,
                    source_type="user_input",
                    source_id="user",
                ),
                model_suggestion=bool(args.get("model_suggestion")),
                retrieved_content=bool(args.get("retrieved_content")),
                explicit_user_authorized=bool(args.get("explicit_user_authorized")),
            )
            decision = self._memory.propose_write(req)
            return {"decision": decision.decision, "reason": decision.reason}
        raise ToolNotFoundError("operation_not_supported")
