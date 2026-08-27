"""Bounded batching helpers for large acquisition workloads via TaskQueue."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AcquisitionBatchPlan:
    """Plan for enqueueing crawl/parse work — does not run network itself."""

    source_id: str
    tenant_id: str
    urls: tuple[str, ...]
    batch_size: int = 20
    workflow_id: str = ""

    def batches(self) -> tuple[tuple[str, ...], ...]:
        size = max(1, int(self.batch_size))
        chunks = []
        for i in range(0, len(self.urls), size):
            chunks.append(tuple(self.urls[i : i + size]))
        return tuple(chunks)

    def execution_keys(self) -> tuple[str, ...]:
        return tuple(
            f"acq-batch:{self.tenant_id}:{self.source_id}:{i}:{len(batch)}"
            for i, batch in enumerate(self.batches())
        )


def plan_crawl_batches(
    *,
    source_id: str,
    tenant_id: str,
    urls: tuple[str, ...],
    batch_size: int = 20,
    workflow_id: str = "",
    max_urls: int = 500,
) -> AcquisitionBatchPlan:
    bounded = tuple(urls[: max(0, int(max_urls))])
    return AcquisitionBatchPlan(
        source_id=source_id,
        tenant_id=tenant_id,
        urls=bounded,
        batch_size=batch_size,
        workflow_id=workflow_id,
    )
