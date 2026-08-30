# Block 10 — Product Media Intelligence bypass audit

## Scope
Production-reachable paths from Agent / Workflow / ContentFactory through ToolGateway to media operations.

## Findings (implementation phase)

| Check | Status | Evidence |
|-------|--------|----------|
| Agent → media provider direct | PASS | No OpenAI/Stability/Adobe SDK in `product_media/` |
| ContentFactory → provider SDK direct | PASS | `content_intel/service.generate_media` delegates to `ProductMediaService` |
| Agent → filesystem media writes | PASS | Bytes stored via `SqliteMediaStore` blob table only |
| Agent → similarity index direct | PASS | `TenantSimilarityIndex` accessed only from `ProductMediaService` |
| ProductMediaService → arbitrary HTTP fetch | PASS | No URL fetch in `product_media/` |
| Global similarity + post-filter | PASS | `TenantSimilarityIndex.find_similar` filters by tenant before candidates |
| Separate media queue | PASS | Bulk uses existing `media_large` / `media_bulk` lanes in `task_queue/lanes.py` |
| Heavy sync escape | PASS | `assert_sync_media_allowed` raises `MediaBatchRequired` |
| Raw media telemetry | PASS | `MediaObservability.emit` strips bytes/base64 |
| Deleted media index residue | PASS | `delete()` calls `similarity.remove_version` before tombstone |

## Canonical path
ContentFactory / ToolGateway → `ProductMediaToolAdapter` → `ProductMediaService` → validation/transform/providers → `SqliteMediaStore` → `MediaAssetVersion`

## Residual P2
- Production GPU video generation providers
- Advanced segmentation / CLIP embeddings
- Live marketplace validators
