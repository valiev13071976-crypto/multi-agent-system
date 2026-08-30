"""Knowledge lifecycle — deletion, tombstone, retention."""

from __future__ import annotations

from knowledge.errors import KNOWLEDGE_DELETE_DENIED, KnowledgeError
from knowledge.index import KnowledgeIndex
from knowledge.platform_models import DeletionReceipt, DeletionRequest, STATUS_TOMBSTONED
from knowledge.store import KnowledgeStore
from memory.models import MemoryScope
from security.tenant import normalize_tenant_id, tenants_match


class KnowledgeLifecycleService:
    def __init__(
        self,
        store: KnowledgeStore,
        index: KnowledgeIndex | None = None,
    ):
        self.store = store
        self.index = index

    def delete(
        self,
        request: DeletionRequest,
        *,
        requesting_scope: MemoryScope,
    ) -> DeletionReceipt:
        req_tenant = normalize_tenant_id(requesting_scope.tenant_ref)
        if request.scope is not None:
            if not tenants_match(requesting_scope.tenant_ref, request.scope.tenant_ref):
                raise KnowledgeError(KNOWLEDGE_DELETE_DENIED)
            if requesting_scope.key() != request.scope.key():
                raise KnowledgeError(KNOWLEDGE_DELETE_DENIED)
        if not tenants_match(req_tenant, request.tenant_ref):
            raise KnowledgeError(KNOWLEDGE_DELETE_DENIED)

        version_ids: list[str] = []
        if request.target_version_id:
            version_ids = [request.target_version_id]
        elif request.target_knowledge_id or request.target_source_id:
            active = self.store.list_active_versions(
                tenant_ref=req_tenant,
                scope=requesting_scope,
                source_id=request.target_source_id,
            )
            for v in active:
                if request.target_knowledge_id and v.knowledge_id != request.target_knowledge_id:
                    continue
                version_ids.append(v.version_id)
            if request.target_knowledge_id and not version_ids:
                raise KnowledgeError(KNOWLEDGE_DELETE_DENIED)

        receipt = self.store.tombstone_knowledge(
            tenant_ref=req_tenant,
            knowledge_id=request.target_knowledge_id,
            source_id=request.target_source_id,
            version_id=request.target_version_id,
        )
        if self.index is not None:
            for vid in version_ids:
                self.index.tombstone_version(tenant_ref=req_tenant, version_id=vid)

        return DeletionReceipt(
            deletion_id=receipt.deletion_id,
            tenant_ref=receipt.tenant_ref,
            status=STATUS_TOMBSTONED,
            affected_versions=receipt.affected_versions,
            affected_chunks=receipt.affected_chunks,
            affected_index_records=receipt.affected_index_records,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
        )
