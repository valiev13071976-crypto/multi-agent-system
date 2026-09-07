"""Tool Platform adapter for the Data Acquisition & Parsing Platform (5.2).

Exposes ``scrape.extract`` as the single chat-facing acquisition entry point.
Panda -- never the user -- decides fetch vs. crawl / interactive vs. batch:

- a single URL with ``max_pages<=1`` -> ``MODE_SINGLE``, executed inline
  through the existing ``AcquisitionService`` (fetch -> parse -> normalize ->
  dedupe -> ingest) and returned in the same turn;
- ``max_pages>1`` -> ``MODE_CRAWL``, which the pre-existing
  ``AcquisitionPlanner`` ALWAYS hard-stamps batch/bulk (Block 3 rule, not
  redesigned here) -- submitted through the existing durable workflow
  runtime (``acquisition.crawl_process``, see ``acquisition/workflow_def.py``)
  and reported back as ``BATCH_QUEUED``, mirroring ``data_intel.tools``.

Extracted/normalized records are bridged into a Block 5.1 Data Intelligence
dataset (``DataIntelligenceService.from_acquisition_records``) so the
conversation can continue with filter/dedupe/Excel-export turns without
re-acquiring the source pages (see ``business_assistant/action_continuation``
``FAMILY_ACQUISITION`` -> ``FAMILY_EXCEL`` handoff).
"""

from __future__ import annotations

import hashlib

from acquisition.errors import AcquisitionError
from acquisition.models import CrawlPolicy, DEDUPE_EXACT, DEDUPE_SAME_SOURCE, MODE_SINGLE
from acquisition.web_source import get_or_create_ephemeral_source
from tools.errors import ToolArgumentInvalidError, ToolError, ToolNotFoundError
from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

DEFAULT_MAX_PAGES = 1
# Hard ceiling on user-requested page counts — the actual crawl bound is the
# ephemeral source's CrawlPolicy.max_pages passed straight through, this is
# just a sanity cap so a single chat turn cannot request an unbounded crawl.
MAX_REQUESTABLE_PAGES = 200
MAX_RECORDS_PREVIEW = 50

ACQUISITION_CRAWL_WORKFLOW_TYPE = "acquisition.crawl_process"
ACQUISITION_CRAWL_WORKFLOW_VERSION = "1"

_ACCEPTED_DEDUPE_DECISIONS = {"unique", "possible"}
# Kept for readability/imports elsewhere — same-source/exact dupes are dropped.
_REJECTED_DEDUPE_DECISIONS = {DEDUPE_EXACT, DEDUPE_SAME_SOURCE}


class AcquisitionToolAdapter:
    """Adapter for ``scrape.extract`` — the Block 5.2 chat-facing tool."""

    adapter_id = "acquisition"

    def __init__(self, service=None, *, workflow_runtime=None, data_intelligence=None):
        self._svc = service
        self._workflow_runtime = workflow_runtime
        self._data_intel = data_intelligence

    def supports(self, tool_id: str) -> bool:
        return tool_id == "scrape.extract"

    def health(self) -> str:
        return ADAPTER_HEALTHY if self._svc is not None else ADAPTER_UNAVAILABLE

    @staticmethod
    def _tenant(request) -> str:
        return str(getattr(request, "tenant_id", "") or "legacy-default")

    async def execute_read(self, request, context) -> dict:
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        if request.operation != "extract":
            raise ToolArgumentInvalidError()
        tenant = self._tenant(request)
        url = str(args.get("url") or "").strip()
        if not url:
            raise ToolArgumentInvalidError()
        max_pages = max(1, min(int(args.get("max_pages") or DEFAULT_MAX_PAGES), MAX_REQUESTABLE_PAGES))
        extraction_plan = dict(args.get("extraction_plan") or {})
        conversation_id = str(args.get("conversation_id") or "")
        try:
            if max_pages <= 1:
                return await self._extract_single(url, tenant=tenant, extraction_plan=extraction_plan)
            return await self._extract_crawl(
                url,
                tenant=tenant,
                max_pages=max_pages,
                extraction_plan=extraction_plan,
                conversation_id=conversation_id,
            )
        except AcquisitionError as exc:
            raise ToolError(exc.error_code) from exc

    async def _extract_single(self, url: str, *, tenant: str, extraction_plan: dict) -> dict:
        source = get_or_create_ephemeral_source(
            self._svc, tenant_id=tenant, url=url, max_pages=1, max_depth=0
        )
        planned = self._svc.plan_job(
            source_id=source.source_id,
            tenant_id=tenant,
            mode=MODE_SINGLE,
            seeds=(url,),
            estimated_pages=1,
            crawl_policy=CrawlPolicy(max_depth=0, max_pages=1),
            metadata={"extraction_plan": extraction_plan} if extraction_plan else None,
        )
        if hasattr(self._svc.store, "save_job"):
            self._svc.store.save_job(planned.job)
        result = await self._svc.run_job(
            planned.job, seeds=(url,), dataset_name="acquisition", process=True
        )
        return self._bridge_result(result, tenant=tenant, url=url, workload_class="interactive")

    async def _extract_crawl(
        self,
        url: str,
        *,
        tenant: str,
        max_pages: int,
        extraction_plan: dict,
        conversation_id: str,
    ) -> dict:
        if self._workflow_runtime is None:
            raise ToolError("acquisition_batch_runtime_unavailable")
        source = get_or_create_ephemeral_source(
            self._svc, tenant_id=tenant, url=url, max_pages=max_pages, max_depth=max_pages
        )
        exec_key = "acq-crawl:" + hashlib.sha256(
            f"{tenant}:{source.source_id}:{url}:{max_pages}".encode("utf-8")
        ).hexdigest()[:32]
        created = await self._workflow_runtime.create_and_enqueue(
            ACQUISITION_CRAWL_WORKFLOW_TYPE,
            ACQUISITION_CRAWL_WORKFLOW_VERSION,
            execution_key=exec_key,
            tenant_id=tenant,
            metadata={
                "source_id": source.source_id,
                "seed_url": url,
                "max_pages": max_pages,
                "extraction_plan": extraction_plan,
                "conversation_id": conversation_id,
            },
        )
        workflow_id = created["workflow_id"] if isinstance(created, dict) else created.workflow_id
        return {
            "status": "BATCH_QUEUED",
            "workflow_id": workflow_id,
            "workload_class": "batch",
            "url": url,
            "max_pages": max_pages,
            "summary_text": (
                f"Собираю до {max_pages} страниц — задача большая, выполняю в фоне "
                "и пришлю результат отдельно."
            ),
        }

    def _bridge_result(self, result, *, tenant: str, url: str, workload_class: str) -> dict:
        bridged = bridge_pipeline_result_to_dataset(
            result, data_intelligence=self._data_intel, tenant_id=tenant
        )
        return {
            "status": "OK",
            "url": url,
            "workload_class": workload_class,
            **bridged,
        }


def bridge_pipeline_result_to_dataset(result, *, data_intelligence, tenant_id: str) -> dict:
    """Shared bridge used by both the interactive path (above) and the
    ``acquisition.crawl_process`` batch workflow — Acquisition ``ParsedRecord``s
    become a Block 5.1 dataset via ``from_acquisition_records`` (never a
    second/duplicate Excel-facing dataset implementation)."""

    decisions_by_record = {d.record_id: d for d in result.decisions}
    rows: list[dict] = []
    for rec in result.normalized:
        decision = decisions_by_record.get(rec.record_id)
        if decision is not None and decision.decision not in _ACCEPTED_DEDUPE_DECISIONS:
            continue
        rows.append(dict(rec.fields))
    dataset_id = ""
    if data_intelligence is not None and rows:
        bridged = data_intelligence.from_acquisition_records(rows, tenant_id=tenant_id)
        dataset_id = bridged["dataset_id"]
    return {
        "dataset_id": dataset_id,
        "record_count": len(rows),
        "records_preview": rows[:MAX_RECORDS_PREVIEW],
        "job_status": result.status,
    }
