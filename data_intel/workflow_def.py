"""data.large_process workflow — bounded row-batch normalize/profile."""

from __future__ import annotations

from data_intel.cleaning import clean_row
from data_intel.errors import DataIntelError
from data_intel.large import build_row_batch_plan
from data_intel.mapping import role_map
from workflow.definition import (
    FAILURE_RETRY,
    STEP_TYPE_HANDLER,
    StepResult,
    StepRetryPolicy,
    WorkflowDefinition,
    WorkflowStep,
)


def large_process_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="data.large_process",
        version="1",
        timeout_seconds=3600.0,
        steps=(
            WorkflowStep(step_id="data_large_prepare", step_type=STEP_TYPE_HANDLER),
            WorkflowStep(
                step_id="data_large_batch",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("data_large_prepare",),
                retry_policy=StepRetryPolicy(
                    max_attempts=5,
                    base_delay_seconds=0.01,
                    backoff_mode="fixed",
                    retryable_error_classes=("dataset_parse_failed", "DataIntelError"),
                ),
                failure_policy=FAILURE_RETRY,
            ),
            WorkflowStep(
                step_id="data_large_finalize",
                step_type=STEP_TYPE_HANDLER,
                dependencies=("data_large_batch",),
            ),
        ),
    )


def _services(ctx):
    platform = ctx["platform"]
    engine = getattr(platform, "workflow_engine", None)
    return getattr(engine, "data_intelligence", None) if engine else None


async def data_large_process_handler(ctx) -> StepResult:
    step = ctx["step"]
    state = ctx["state"]
    meta = dict(state.metadata or {})
    dataset_id = str(meta.get("dataset_id") or "")
    tenant_id = str(meta.get("tenant_id") or "legacy-default")
    svc = _services(ctx)

    if step.step_id == "data_large_prepare":
        row_count = int(meta.get("row_count") or 0)
        batch_size = int(meta.get("rows_per_batch") or 500)
        if svc is not None:
            batch_size = int(getattr(svc.large_policy, "rows_per_batch", batch_size) or batch_size)
        plan = build_row_batch_plan(
            dataset_id=dataset_id,
            tenant_id=tenant_id,
            row_count=row_count,
            rows_per_batch=batch_size,
        )
        return StepResult(
            ok=True,
            data={"batch_count": plan["batch_count"], "batches": plan["batches"]},
            result_ref=f"dataset:{dataset_id}",
        )

    if step.step_id == "data_large_batch":
        if svc is None:
            raise DataIntelError("dataset_store_unavailable")
        prep = state.step("data_large_prepare")
        batches = list((prep.metadata.get("result") or {}).get("batches") or meta.get("batches") or ())
        partials = svc.store.list_partials(dataset_id, tenant_id=tenant_id)
        completed = {int(i) for i, p in partials.items() if p.get("status") == "completed"}
        batch = None
        for b in batches:
            if int(b["batch_index"]) not in completed:
                batch = b
                break
        if batch is None:
            return StepResult(ok=True, data={"batches_remaining": 0}, result_ref=f"dataset:{dataset_id}")

        rows = svc.store.get_rows(dataset_id, tenant_id=tenant_id)
        start = int(batch["row_start"])
        end = int(batch["row_end"])
        # Bound check
        if end - start > int(getattr(svc.large_policy, "rows_per_batch", 500) or 500):
            raise DataIntelError("dataset_too_large")
        slice_rows = rows[start:end]
        desc = svc.store.get_dataset(dataset_id, tenant_id=tenant_id)
        roles = {}
        if desc and desc.tables:
            roles = role_map(desc.tables[0].columns)
        cleaned = []
        for r in slice_rows:
            values = {k: v for k, v in r.items() if not str(k).startswith("__")}
            c, raw = clean_row(values, roles=roles)
            c["__source_row"] = r.get("__source_row")
            c["__raw"] = raw
            cleaned.append(c)
        payload = {
            "batch_index": int(batch["batch_index"]),
            "row_start": start,
            "row_end": end,
            "status": "completed",
            "row_count": len(cleaned),
            "rows": cleaned,
            "bounded": True,
        }
        svc.store.save_partial(dataset_id, int(batch["batch_index"]), payload, tenant_id=tenant_id)
        completed.add(int(batch["batch_index"]))
        remaining = len(batches) - len(completed)
        data = {
            "batch_index": int(batch["batch_index"]),
            "completed_batches": sorted(completed),
            "batches_remaining": remaining,
            "bounded": True,
            "rows_processed": len(cleaned),
        }
        if remaining > 0:
            data["continue_step"] = True
        return StepResult(ok=True, data=data, result_ref=f"dataset:{dataset_id}")

    if step.step_id == "data_large_finalize":
        if svc is None:
            raise DataIntelError("dataset_store_unavailable")
        partials = svc.store.list_partials(dataset_id, tenant_id=tenant_id)
        merged = []
        for idx in sorted(partials.keys()):
            p = partials[idx]
            if p.get("status") != "completed":
                raise DataIntelError("dataset_parse_failed")
            merged.extend(list(p.get("rows") or ()))
        desc = svc.store.get_dataset(dataset_id, tenant_id=tenant_id)
        table_id = desc.tables[0].table_id if desc and desc.tables else "main"
        # Persist merged cleaned rows
        from dataclasses import replace

        if desc is not None:
            updated = replace(desc, status="normalized", row_count=len(merged))
            svc.store.save_dataset(updated, {table_id: merged})
        return StepResult(
            ok=True,
            data={"merged_rows": len(merged), "status": "normalized"},
            result_ref=f"dataset:{dataset_id}",
        )

    return StepResult(ok=True, data={"step_id": step.step_id})


def register_data_intel_workflows(definitions, platform) -> None:
    try:
        definitions.register(large_process_definition())
    except Exception:
        pass
    for step_id in ("data_large_prepare", "data_large_batch", "data_large_finalize"):
        platform.register_handler(step_id, data_large_process_handler)
