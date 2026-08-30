"""DataPlanner — trusted TaskQueue metadata for Excel / Data Intelligence workloads.

Large datasets / heavy tabular ops ALWAYS stamp batch admission metadata so they
cannot run on the interactive pool even if the caller forces an interactive hint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from data_intel.errors import DATASET_BATCH_REQUIRED, DataIntelError
from security.tenant import require_tenant_id
from task_queue.lanes import (
    LANE_BULK,
    WORKLOAD_BATCH,
    WORKLOAD_NORMAL,
    classify_workload,
)

# Canonical thresholds — align with LargeDatasetPolicy.max_sync_rows.
LARGE_SYNC_ROWS = 5_000
LARGE_BATCH_ROWS = 300_000  # spec 7.12 acceptance threshold
LARGE_DATA_BYTES = 50 * 1024 * 1024  # 50 MiB

TRUSTED_JOB_DATA_LARGE = "data_large"
TRUSTED_JOB_EXCEL_LARGE = "excel_large"

DATA_OPERATIONS = frozenset(
    {
        "ingest",
        "normalize",
        "match",
        "dedup",
        "compare",
        "reconcile",
        "merge",
        "analyze",
        "generate_xlsx",
    }
)
HEAVY_OPS = frozenset({"match", "dedup", "compare", "reconcile", "merge", "analyze", "generate_xlsx"})


@dataclass(frozen=True)
class PlannedDataJob:
    trusted_metadata: Mapping[str, object]
    execution_lane: str
    workload_class: str
    enqueue: bool
    reason: str = ""


def assert_hard_batch_admission(trusted_metadata: Mapping[str, object]) -> None:
    meta = dict(trusted_metadata or {})
    stamped = classify_workload(metadata=meta)
    jt = str(meta.get("trusted_job_type") or "")
    if jt in {TRUSTED_JOB_DATA_LARGE, TRUSTED_JOB_EXCEL_LARGE}:
        if stamped.lane != LANE_BULK or str(meta.get("workload_class")) != WORKLOAD_BATCH:
            raise RuntimeError("data_batch_admission_stamp_failed")
        if str(meta.get("execution_lane")) != LANE_BULK:
            raise RuntimeError("data_batch_admission_stamp_failed")


def _must_batch(
    *,
    operations: Sequence[str],
    row_count: int | None,
    byte_size: int | None,
    bulk: bool,
) -> tuple[bool, str]:
    ops = {str(o).lower() for o in operations}
    rows = int(row_count) if row_count is not None else None
    size = int(byte_size) if byte_size is not None else None

    if bulk:
        return True, TRUSTED_JOB_DATA_LARGE
    if rows is not None and rows >= LARGE_BATCH_ROWS:
        return True, TRUSTED_JOB_DATA_LARGE
    if rows is not None and rows >= LARGE_SYNC_ROWS and ops & HEAVY_OPS:
        return True, TRUSTED_JOB_DATA_LARGE
    if size is not None and size >= LARGE_DATA_BYTES:
        return True, TRUSTED_JOB_DATA_LARGE
    if ops & {"generate_xlsx"} and rows is not None and rows >= LARGE_SYNC_ROWS:
        return True, TRUSTED_JOB_EXCEL_LARGE
    return False, ""


def plan_data_job(
    *,
    dataset_id: str,
    tenant_id: str,
    operations: Sequence[str],
    row_count: int | None = None,
    byte_size: int | None = None,
    bulk: bool = False,
    force_interactive_hint: bool = False,
    metadata: Mapping[str, object] | None = None,
) -> PlannedDataJob:
    """Build trusted TaskQueue metadata for data workloads."""
    tid = require_tenant_id(tenant_id)
    ops = tuple(o for o in (str(x).lower() for x in operations) if o in DATA_OPERATIONS)
    if not ops:
        ops = ("ingest",)

    batch, job_type = _must_batch(
        operations=ops,
        row_count=row_count,
        byte_size=byte_size,
        bulk=bool(bulk),
    )
    _ = force_interactive_hint  # caller hint MUST NOT win

    if batch:
        if not job_type:
            job_type = TRUSTED_JOB_DATA_LARGE
        trusted_metadata = {
            "trusted_job_type": job_type,
            "workload_class": WORKLOAD_BATCH,
            "execution_lane": LANE_BULK,
            "dataset_id": dataset_id,
            "data_operations": list(ops),
        }
        if metadata:
            for k, v in dict(metadata).items():
                if k not in {"trusted_job_type", "workload_class", "execution_lane"}:
                    trusted_metadata[k] = v
        assert_hard_batch_admission(trusted_metadata)
        return PlannedDataJob(
            trusted_metadata=trusted_metadata,
            execution_lane=LANE_BULK,
            workload_class=WORKLOAD_BATCH,
            enqueue=True,
            reason="batch_required",
        )

    trusted_metadata = {
        "trusted_job_type": "data",
        "workload_class": WORKLOAD_NORMAL,
        "execution_lane": "default",
        "dataset_id": dataset_id,
        "data_operations": list(ops),
    }
    stamped = classify_workload(metadata=trusted_metadata)
    return PlannedDataJob(
        trusted_metadata=trusted_metadata,
        execution_lane=stamped.lane,
        workload_class=stamped.name,
        enqueue=False,
        reason="inline_ok",
    )


def requires_batch_data_admission(
    *,
    row_count: int | None,
    byte_size: int | None = None,
    operations: Sequence[str] = ("ingest",),
) -> bool:
    batch, _ = _must_batch(
        operations=tuple(operations),
        row_count=row_count,
        byte_size=byte_size,
        bulk=False,
    )
    return batch


def assert_sync_data_allowed(
    *,
    row_count: int | None,
    byte_size: int | None = None,
    operations: Sequence[str] = ("ingest",),
    require_known_size: bool = True,
) -> None:
    """Fail closed when synchronous data processing is not permitted."""
    if require_known_size and row_count is None and byte_size is None:
        raise DataIntelError(DATASET_BATCH_REQUIRED)
    if requires_batch_data_admission(
        row_count=row_count, byte_size=byte_size, operations=operations
    ):
        raise DataIntelError(DATASET_BATCH_REQUIRED)
