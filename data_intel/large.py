"""Large dataset policy — bounded batches, no whole-table LLM prompts."""

from __future__ import annotations

from dataclasses import dataclass

from security.tenant import normalize_tenant_id


@dataclass(frozen=True)
class LargeDatasetPolicy:
    max_sync_rows: int = 5_000
    max_sync_cells: int = 200_000
    max_sync_bytes: int = 5_000_000
    rows_per_batch: int = 500
    max_memory_estimate_mb: int = 256

    def requires_async(self, *, row_count: int = 0, cell_count: int = 0, size_bytes: int = 0) -> bool:
        if row_count > self.max_sync_rows:
            return True
        if cell_count > self.max_sync_cells:
            return True
        if size_bytes > self.max_sync_bytes:
            return True
        return False


def large_dataset_execution_key(tenant_id: str, dataset_id: str, *, version: str = "1") -> str:
    tid = normalize_tenant_id(tenant_id)
    return f"data-intel:{tid}:{dataset_id}:v{version}"


def build_row_batch_plan(
    *,
    dataset_id: str,
    tenant_id: str,
    row_count: int,
    rows_per_batch: int = 500,
) -> dict:
    size = max(1, int(rows_per_batch))
    total = max(0, int(row_count))
    batches = []
    for idx, start in enumerate(range(0, total, size)):
        end = min(total, start + size)
        batches.append(
            {
                "batch_index": idx,
                "row_start": start,
                "row_end": end,  # exclusive
                "bounded": True,
                "dataset_id": dataset_id,
                "tenant_id": tenant_id,
            }
        )
    return {
        "dataset_id": dataset_id,
        "tenant_id": tenant_id,
        "batch_count": len(batches),
        "batches": batches,
        "workflow_type": "data.large_process",
        "execution_key": large_dataset_execution_key(tenant_id, dataset_id),
        "strategy": "row_batches",
    }
