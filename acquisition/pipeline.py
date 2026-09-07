"""End-to-end acquisition pipeline: fetch → parse → normalize → dedupe → ingest."""

from __future__ import annotations

from dataclasses import dataclass, replace

from acquisition.dedupe import DedupeEngine
from acquisition.ingest import InMemoryIngestionTarget, SqliteIngestionTarget
from acquisition.models import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_PARTIAL,
    JOB_RUNNING,
    AcquisitionJob,
    DatasetResult,
    DedupeDecision,
    IngestionBatchResult,
    NormalizedRecord,
    utc_now,
)
from acquisition.normalize import RecordNormalizer
from acquisition.observability import get_observer


@dataclass
class PipelineResult:
    job: AcquisitionJob
    normalized: tuple[NormalizedRecord, ...]
    decisions: tuple[DedupeDecision, ...]
    ingest: IngestionBatchResult | None
    dataset: DatasetResult | None
    status: str


class AcquisitionPipeline:
    """Governed path after raw artifacts exist: parse→normalize→dedupe→ingest."""

    def __init__(
        self,
        *,
        service,
        normalizer: RecordNormalizer | None = None,
        dedupe: DedupeEngine | None = None,
        ingest_target=None,
    ):
        self.service = service
        self.normalizer = normalizer or RecordNormalizer()
        self.dedupe = dedupe or DedupeEngine()
        if ingest_target is not None:
            self.ingest = ingest_target
        elif hasattr(service.store, "save_ingest_batch"):
            self.ingest = SqliteIngestionTarget(service.store)
        else:
            self.ingest = InMemoryIngestionTarget()
        self.observer = get_observer()

    def process_artifacts(
        self,
        *,
        job: AcquisitionJob,
        artifacts,
        dataset_name: str = "default",
        idempotency_key: str = "",
    ) -> PipelineResult:
        if job.cancel_requested:
            return PipelineResult(
                job=replace(job, status=JOB_CANCELLED, updated_at=utc_now()),
                normalized=(),
                decisions=(),
                ingest=None,
                dataset=None,
                status=JOB_CANCELLED,
            )

        running = replace(job, status=JOB_RUNNING, started_at=job.started_at or utc_now(), updated_at=utc_now())
        if hasattr(self.service.store, "save_job"):
            self.service.store.save_job(running)

        normalized: list[NormalizedRecord] = []
        decisions: list[DedupeDecision] = []
        for art in artifacts:
            records = tuple(self.service.parse(art))
            # A parser may legitimately emit MULTIPLE records from a single
            # artifact (e.g. repeated-item/card extraction, marketplace/search
            # listings). The artifact-level URL/checksum identify the PAGE, not
            # any individual record on it -- keying dedupe layer 1 (url) and
            # layer 2 (raw_hash) off the shared page URL/checksum for every
            # record would falsely collapse distinct same-page records into
            # "duplicates" of each other. Use a per-record URL (when the
            # record itself carries one) for multi-record artifacts, and skip
            # the page-level raw_hash layer entirely for them -- layers 3/4
            # (structured fingerprint / composite key) still dedupe correctly
            # per-record. Single-record artifacts keep the original page-level
            # identity (unchanged behavior).
            multi_record = len(records) > 1
            for rec in records:
                result = self.normalizer.normalize_parsed(
                    rec, job_id=job.job_id, resource_id=art.artifact_id
                )
                normalized.append(result.record)
                if hasattr(self.service.store, "save_normalized_record"):
                    self.service.store.save_normalized_record(result.record)
                if multi_record:
                    rec_fields = dict(result.record.fields)
                    dedupe_url = str(rec_fields.get("url") or rec_fields.get("link") or "")
                    # A record's own "url"/"link" field only identifies that
                    # SPECIFIC record (e.g. a card's product-detail link) when
                    # it differs from the shared page URL. Generic table-row
                    # extraction (acquisition/parsers/web_generic.py) stamps
                    # every row with the same page URL for provenance -- using
                    # that shared value as a per-record dedupe key here would
                    # falsely collapse distinct rows on the same page into
                    # "duplicates" of each other (layer 1). Fall through to
                    # the structured-fingerprint layer (3), which already
                    # incorporates every field and correctly distinguishes
                    # same-page records, whenever the URL isn't record-unique.
                    if dedupe_url and dedupe_url == (art.url or ""):
                        dedupe_url = ""
                    dedupe_raw_hash = ""
                else:
                    dedupe_url = art.url or ""
                    dedupe_raw_hash = art.checksum or ""
                decision = self.dedupe.decide(
                    result.record,
                    job_id=job.job_id,
                    url=dedupe_url,
                    raw_hash=dedupe_raw_hash,
                )
                decisions.append(decision)

        self.observer.metrics.records_normalized += len(normalized)
        self.observer.metrics.records_deduped += len(decisions)

        unique_for_ingest = tuple(
            n
            for n, d in zip(normalized, decisions)
            if d.decision == "unique" or d.decision == "possible"
        )
        # Also pass all with decisions so duplicates are counted
        batch = self.ingest.ingest_batch(
            tenant_id=job.tenant_id,
            job_id=job.job_id,
            dataset_name=dataset_name,
            records=tuple(normalized),
            decisions=tuple(decisions),
            idempotency_key=idempotency_key or f"ingest:{job.job_id}:{dataset_name}",
        )
        self.observer.metrics.ingest_accepted += batch.accepted
        self.observer.metrics.ingest_duplicate += batch.duplicate

        dataset = self.ingest.get_dataset(batch.dataset_id, tenant_id=job.tenant_id)
        status = JOB_COMPLETED
        if batch.failed or batch.rejected:
            status = JOB_PARTIAL
        done = replace(
            running,
            status=status,
            completed_at=utc_now(),
            updated_at=utc_now(),
            counters={
                **dict(running.counters),
                "normalized": len(normalized),
                "ingest_accepted": batch.accepted,
                "ingest_duplicate": batch.duplicate,
                "ingest_rejected": batch.rejected,
                "ingest_failed": batch.failed,
            },
        )
        if hasattr(self.service.store, "save_job"):
            self.service.store.save_job(done)
        self.observer.on_job_completed(job_id=job.job_id, tenant_id=job.tenant_id, status=status)
        return PipelineResult(
            job=done,
            normalized=tuple(normalized),
            decisions=tuple(decisions),
            ingest=batch,
            dataset=dataset,
            status=status,
        )
