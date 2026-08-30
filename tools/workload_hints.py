"""Map tool operations to workload classes via task_queue.lanes."""

from __future__ import annotations

from typing import Mapping

from task_queue.lanes import (
    WORKLOAD_BACKGROUND,
    WORKLOAD_BATCH,
    WORKLOAD_INTERACTIVE,
    WORKLOAD_NORMAL,
    WorkloadClass,
    classify_workload,
    route_heavy_job,
)
from tools.models import (
    WORKLOAD_HINT_BACKGROUND,
    WORKLOAD_HINT_BATCH,
    WORKLOAD_HINT_INTERACTIVE,
    WORKLOAD_HINT_NORMAL,
    ToolDescriptor,
)

# Tool/category → heavy job type or workload hint
_BATCH_TOOL_PREFIXES = (
    "excel.",
    "scrape.",
    "data.generate_excel",
    "document.ocr",
)
_BATCH_CATEGORIES = frozenset({"excel", "scrape", "crawler", "ocr"})
_BACKGROUND_PREFIXES = ("image.", "media.", "document.generate")
_BACKGROUND_CATEGORIES = frozenset({"image", "media"})
_INTERACTIVE_PREFIXES = ("search", "web_search", "web.search")
_INTERACTIVE_CATEGORIES = frozenset({"search", "web_search"})


def hint_for_tool(
    descriptor: ToolDescriptor | None = None,
    *,
    tool_id: str = "",
    operation: str = "",
    category: str = "",
    estimated_rows: int | None = None,
    estimated_bytes: int | None = None,
    metadata: Mapping | None = None,
) -> WorkloadClass:
    """Classify tool work into interactive/normal/batch/background."""
    tid = str(tool_id or (descriptor.tool_id if descriptor else "") or "")
    cat = str(category or (descriptor.category if descriptor else "") or "")
    op = str(operation or "").lower()
    meta = dict(metadata or {})

    # Descriptor hint wins when present
    if descriptor and descriptor.workload_class_hint:
        return classify_workload(
            metadata={"workload_class": descriptor.workload_class_hint, **meta}
        )

    # Large excel / ocr / scrape → batch
    if estimated_rows is not None or estimated_bytes is not None:
        return classify_workload(
            estimated_rows=estimated_rows,
            estimated_bytes=estimated_bytes,
            metadata=meta,
        )

    lower_tid = tid.lower()
    if any(lower_tid.startswith(p) for p in _BATCH_TOOL_PREFIXES) or cat in _BATCH_CATEGORIES:
        if "ocr" in lower_tid or op == "ocr" or cat == "ocr":
            return classify_workload(job_type="excel_large", metadata=meta)  # batch lane
        if "scrape" in lower_tid or cat in {"scrape", "crawler"}:
            return classify_workload(job_type="crawler", metadata=meta)
        return classify_workload(job_type="excel_large", metadata=meta)

    if any(lower_tid.startswith(p) for p in _BACKGROUND_PREFIXES) or cat in _BACKGROUND_CATEGORIES:
        return classify_workload(job_type="media", metadata=meta)

    if any(lower_tid.startswith(p) or lower_tid == p for p in _INTERACTIVE_PREFIXES) or cat in _INTERACTIVE_CATEGORIES:
        # Search is interactive/normal depending on priority metadata
        if meta.get("priority") in {"high", "critical"}:
            return classify_workload(priority="high", metadata=meta)
        return classify_workload(priority="normal", metadata={**meta, "workload_class": WORKLOAD_NORMAL})

    if descriptor and descriptor.workload_class_hint == WORKLOAD_HINT_BATCH:
        return WorkloadClass(WORKLOAD_BATCH, route_heavy_job("excel_large"))
    if descriptor and descriptor.workload_class_hint == WORKLOAD_HINT_BACKGROUND:
        return WorkloadClass(WORKLOAD_BACKGROUND, route_heavy_job("media"))
    if descriptor and descriptor.workload_class_hint == WORKLOAD_HINT_INTERACTIVE:
        return WorkloadClass(WORKLOAD_INTERACTIVE, "interactive")
    if descriptor and descriptor.workload_class_hint == WORKLOAD_HINT_NORMAL:
        return WorkloadClass(WORKLOAD_NORMAL, "background")

    return classify_workload(metadata=meta)


def workload_hint_name(descriptor: ToolDescriptor | None = None, **kwargs) -> str:
    return hint_for_tool(descriptor, **kwargs).name
