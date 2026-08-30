"""In-memory tenant-scoped structured acquisition store."""

from __future__ import annotations

from acquisition.models import (
    AcquiredResource,
    AcquisitionJob,
    ChangeEvent,
    CrawlCheckpoint,
    DatasetResult,
    FrontierEntry,
    IngestionBatchResult,
    NormalizedRecord,
    ParsedRecord,
    RawArtifact,
    SourceDescriptor,
)
from security.tenant import normalize_tenant_id, require_tenant_id, tenants_match


class AcquisitionStore:
    """Interface — in-memory default; sqlite can mirror later."""

    def save_source(self, descriptor: SourceDescriptor) -> None:
        raise NotImplementedError

    def get_source(self, source_id: str, *, tenant_id: str) -> SourceDescriptor | None:
        raise NotImplementedError

    def list_sources(self, *, tenant_id: str) -> tuple[SourceDescriptor, ...]:
        raise NotImplementedError

    def save_artifact(self, artifact: RawArtifact) -> RawArtifact:
        raise NotImplementedError

    def find_artifact_by_checksum(
        self, checksum: str, *, tenant_id: str, source_id: str | None = None
    ) -> RawArtifact | None:
        raise NotImplementedError

    def get_artifact(self, artifact_id: str, *, tenant_id: str) -> RawArtifact | None:
        raise NotImplementedError

    def save_record(self, record: ParsedRecord) -> ParsedRecord:
        raise NotImplementedError

    def find_record_by_fingerprint(
        self, fingerprint: str, *, tenant_id: str, source_id: str | None = None
    ) -> ParsedRecord | None:
        raise NotImplementedError

    def list_records(
        self, *, tenant_id: str, source_id: str | None = None, record_type: str | None = None
    ) -> tuple[ParsedRecord, ...]:
        raise NotImplementedError

    def get_record(self, record_id: str, *, tenant_id: str) -> ParsedRecord | None:
        raise NotImplementedError

    def save_change(self, event: ChangeEvent) -> None:
        raise NotImplementedError

    def list_changes(
        self, *, tenant_id: str, source_id: str | None = None, record_id: str | None = None
    ) -> tuple[ChangeEvent, ...]:
        raise NotImplementedError


class InMemoryAcquisitionStore(AcquisitionStore):
    def __init__(self):
        self._sources: dict[tuple[str, str], SourceDescriptor] = {}
        self._artifacts: dict[str, RawArtifact] = {}
        self._records: dict[str, ParsedRecord] = {}
        self._changes: list[ChangeEvent] = []
        self._checksum_index: dict[tuple[str, str], str] = {}
        self._fp_index: dict[tuple[str, str], str] = {}
        self._jobs: dict[str, AcquisitionJob] = {}
        self._frontier: dict[str, FrontierEntry] = {}
        self._checkpoints: dict[str, CrawlCheckpoint] = {}
        self._resources: dict[str, AcquiredResource] = {}
        self._normalized: dict[str, NormalizedRecord] = {}
        self._datasets: dict[str, DatasetResult] = {}
        self._ingest_batches: dict[str, IngestionBatchResult] = {}
        self._ingest_idem: dict[tuple[str, str], str] = {}

    def save_source(self, descriptor: SourceDescriptor) -> None:
        self._sources[(descriptor.tenant_id, descriptor.source_id)] = descriptor

    def get_source(self, source_id: str, *, tenant_id: str) -> SourceDescriptor | None:
        return self._sources.get((normalize_tenant_id(tenant_id), source_id))

    def list_sources(self, *, tenant_id: str) -> tuple[SourceDescriptor, ...]:
        tid = normalize_tenant_id(tenant_id)
        return tuple(d for (t, _), d in self._sources.items() if tenants_match(t, tid))

    def save_artifact(self, artifact: RawArtifact) -> RawArtifact:
        existing_id = self._checksum_index.get((artifact.tenant_id, artifact.checksum))
        if existing_id and existing_id in self._artifacts:
            return self._artifacts[existing_id]
        self._artifacts[artifact.artifact_id] = artifact
        self._checksum_index[(artifact.tenant_id, artifact.checksum)] = artifact.artifact_id
        return artifact

    def find_artifact_by_checksum(
        self, checksum: str, *, tenant_id: str, source_id: str | None = None
    ) -> RawArtifact | None:
        tid = normalize_tenant_id(tenant_id)
        aid = self._checksum_index.get((tid, checksum))
        if not aid:
            return None
        art = self._artifacts.get(aid)
        if art is None or not tenants_match(art.tenant_id, tid):
            return None
        if source_id and art.source_id != source_id:
            return None
        return art

    def get_artifact(self, artifact_id: str, *, tenant_id: str) -> RawArtifact | None:
        art = self._artifacts.get(artifact_id)
        if art is None or not tenants_match(art.tenant_id, tenant_id):
            return None
        return art

    def save_record(self, record: ParsedRecord) -> ParsedRecord:
        key = (record.tenant_id, record.fingerprint)
        existing_id = self._fp_index.get(key)
        if existing_id and existing_id in self._records:
            return self._records[existing_id]
        self._records[record.record_id] = record
        self._fp_index[key] = record.record_id
        return record

    def find_record_by_fingerprint(
        self, fingerprint: str, *, tenant_id: str, source_id: str | None = None
    ) -> ParsedRecord | None:
        tid = normalize_tenant_id(tenant_id)
        rid = self._fp_index.get((tid, fingerprint))
        if not rid:
            # also scan for source-scoped previous observations with same natural key later
            for rec in self._records.values():
                if tenants_match(rec.tenant_id, tid) and rec.fingerprint == fingerprint:
                    if source_id is None or rec.source_id == source_id:
                        return rec
            return None
        rec = self._records.get(rid)
        if rec is None or not tenants_match(rec.tenant_id, tid):
            return None
        if source_id and rec.source_id != source_id:
            return None
        return rec

    def list_records(
        self, *, tenant_id: str, source_id: str | None = None, record_type: str | None = None
    ) -> tuple[ParsedRecord, ...]:
        tid = normalize_tenant_id(tenant_id)
        out = []
        for rec in self._records.values():
            if not tenants_match(rec.tenant_id, tid):
                continue
            if source_id and rec.source_id != source_id:
                continue
            if record_type and rec.record_type != record_type:
                continue
            out.append(rec)
        return tuple(out)

    def get_record(self, record_id: str, *, tenant_id: str) -> ParsedRecord | None:
        rec = self._records.get(record_id)
        if rec is None or not tenants_match(rec.tenant_id, tenant_id):
            return None
        return rec

    def find_previous_observation(
        self, *, tenant_id: str, source_id: str, natural_key: str
    ) -> ParsedRecord | None:
        """Find prior record with same source + natural entity key (sku/ean)."""
        tid = normalize_tenant_id(tenant_id)
        for rec in reversed(list(self._records.values())):
            if not tenants_match(rec.tenant_id, tid):
                continue
            if rec.source_id != source_id:
                continue
            fields = dict(rec.fields)
            key = str(
                fields.get("ean")
                or fields.get("sku")
                or fields.get("supplier_sku")
                or fields.get("source_sku")
                or fields.get("mpn")
                or ""
            )
            if key and key == natural_key:
                return rec
        return None

    def save_change(self, event: ChangeEvent) -> None:
        self._changes.append(event)

    def list_changes(
        self, *, tenant_id: str, source_id: str | None = None, record_id: str | None = None
    ) -> tuple[ChangeEvent, ...]:
        tid = normalize_tenant_id(tenant_id)
        out = []
        for ev in self._changes:
            if not tenants_match(ev.tenant_id, tid):
                continue
            if source_id and ev.source_id != source_id:
                continue
            if record_id and ev.record_id != record_id:
                continue
            out.append(ev)
        return tuple(out)

    # --- v2 scale objects (fail-closed tenant) ---
    def save_job(self, job: AcquisitionJob) -> AcquisitionJob:
        require_tenant_id(job.tenant_id)
        self._jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str, *, tenant_id: str) -> AcquisitionJob | None:
        tid = require_tenant_id(tenant_id)
        job = self._jobs.get(job_id)
        if job is None or not tenants_match(job.tenant_id, tid):
            return None
        return job

    def list_jobs(self, *, tenant_id: str, status: str | None = None) -> tuple[AcquisitionJob, ...]:
        tid = require_tenant_id(tenant_id)
        out = []
        for job in self._jobs.values():
            if not tenants_match(job.tenant_id, tid):
                continue
            if status and job.status != status:
                continue
            out.append(job)
        return tuple(out)

    def save_frontier_entry(self, entry: FrontierEntry) -> FrontierEntry:
        require_tenant_id(entry.tenant_id)
        self._frontier[entry.entry_id] = entry
        return entry

    def list_frontier(
        self, *, job_id: str, tenant_id: str, statuses: tuple[str, ...] | None = None
    ) -> tuple[FrontierEntry, ...]:
        tid = require_tenant_id(tenant_id)
        out = []
        for entry in self._frontier.values():
            if entry.job_id != job_id or not tenants_match(entry.tenant_id, tid):
                continue
            if statuses and entry.status not in statuses:
                continue
            out.append(entry)
        return tuple(out)

    def save_checkpoint(self, checkpoint: CrawlCheckpoint) -> CrawlCheckpoint:
        require_tenant_id(checkpoint.tenant_id)
        self._checkpoints[checkpoint.job_id] = checkpoint
        return checkpoint

    def get_checkpoint(self, job_id: str, *, tenant_id: str) -> CrawlCheckpoint | None:
        tid = require_tenant_id(tenant_id)
        cp = self._checkpoints.get(job_id)
        if cp is None or not tenants_match(cp.tenant_id, tid):
            return None
        return cp

    def save_resource(self, resource: AcquiredResource) -> AcquiredResource:
        require_tenant_id(resource.tenant_id)
        self._resources[resource.resource_id] = resource
        return resource

    def list_resources(self, *, job_id: str, tenant_id: str) -> tuple[AcquiredResource, ...]:
        tid = require_tenant_id(tenant_id)
        return tuple(
            r
            for r in self._resources.values()
            if r.job_id == job_id and tenants_match(r.tenant_id, tid)
        )

    def save_normalized_record(self, record: NormalizedRecord) -> NormalizedRecord:
        require_tenant_id(record.tenant_id)
        self._normalized[record.record_id] = record
        return record

    def save_dataset(self, dataset: DatasetResult) -> DatasetResult:
        require_tenant_id(dataset.tenant_id)
        self._datasets[dataset.dataset_id] = dataset
        return dataset

    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetResult | None:
        tid = require_tenant_id(tenant_id)
        ds = self._datasets.get(dataset_id)
        if ds is None or not tenants_match(ds.tenant_id, tid):
            return None
        return ds

    def save_ingest_batch(self, batch: IngestionBatchResult) -> IngestionBatchResult:
        tid = require_tenant_id(batch.tenant_id)
        if batch.idempotency_key:
            existing_id = self._ingest_idem.get((tid, batch.idempotency_key))
            if existing_id and existing_id in self._ingest_batches:
                return self._ingest_batches[existing_id]
            self._ingest_idem[(tid, batch.idempotency_key)] = batch.batch_id
        self._ingest_batches[batch.batch_id] = batch
        return batch
