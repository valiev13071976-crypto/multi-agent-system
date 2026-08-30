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
            records = self.service.parse(art)
            for rec in records:
                result = self.normalizer.normalize_parsed(
                    rec, job_id=job.job_id, resource_id=art.artifact_id
                )
                normalized.append(result.record)
                if hasattr(self.service.store, "save_normalized_record"):
                    self.service.store.save_normalized_record(result.record)
                decision = self.dedupe.decide(
                    result.record,
                    job_id=job.job_id,
                    url=art.url or "",
                    raw_hash=art.checksum or "",
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
