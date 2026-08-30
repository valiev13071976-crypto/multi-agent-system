# Block 8 — Knowledge / RAG / Memory Architecture Bypass Audit

Audit date: implementation phase (Block 8 closure).

## Canonical production path

```
Agent / Workflow
  → ToolGateway (knowledge.* / memory.* tools)
  → KnowledgeService / MemoryService
  → KnowledgeAccessPolicy / MemoryWriteGovernor
  → KnowledgeStore + KnowledgeIndex / MemoryStore
  → bounded RetrievalResult / MemoryRecord
```

Wiring: `knowledge/tools.py` → `tools/platform/bootstrap.py` → `KnowledgeToolAdapter`.

Runtime composition: `knowledge/runtime.py` → `build_knowledge_runtime(store_path=...)`.

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Agent → KnowledgeStore direct | PASS | No agent imports of `knowledge/store` or `sqlite_store` |
| Agent → vector/index direct | PASS | Index accessed only via `KnowledgeService` / `KnowledgeRetrievalService` |
| Agent → embedding provider direct | PASS | `DeterministicEmbeddingProvider` used inside `knowledge/index.py` only |
| Agent → memory DB direct | PASS | Memory writes via `MemoryService` / tool adapter |
| Payload tenant trust | PASS | `KnowledgeAccessPolicy` / `MemoryAccessPolicy` enforce `tenant_ref` |
| Global retrieval + post-filter | PASS | `KnowledgeIndex.search` scopes by tenant before scoring |
| Direct source → index bypass | PASS | Ingestion via `KnowledgeIngestionPipeline` stages |
| Separate knowledge queue | PASS | Reuses `task_queue/lanes.py` `knowledge_large` batch stamp |
| Raw content logging | PASS | `_emit` strips content/query fields |
| Unbounded retrieval | PASS | `limit` capped at 20; chunk diversity enforced |
| Heavy sync escape | PASS | `assert_sync_ingest_allowed` in service + tool adapter |
| Deletion without index cleanup | PASS | `KnowledgeLifecycleService` tombstones store + index |

## Residual P2 (non-blocking)

- Production vector SaaS backend (local SQLite + in-memory index used for closure)
- Distributed reindex worker pool (durable job rows present; worker dispatch deferred)
- Advanced neural reranker (keyword + deterministic vector hybrid only)

## Verdict

No production-reachable bypass paths identified for mandatory Block 8 contracts.
