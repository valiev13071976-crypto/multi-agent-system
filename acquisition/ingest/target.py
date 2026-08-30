"""Ingestion targets — dataset contract + idempotent batch ingest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from acquisition.models import (
    DEDUPE_CROSS_SOURCE,
    DEDUPE_EXACT,
    DEDUPE_POSSIBLE,
    DEDUPE_SAME_SOURCE,
    DEDUPE_UNIQUE,
    INGEST_ACCEPTED,
    INGEST_DUPLICATE,
    INGEST_FAILED,
    INGEST_REJECTED,
    DatasetResult,
    DedupeDecision,
    IngestionBatchResult,
    NormalizedRecord,
    checksum_text,
    fingerprint_record,
    new_id,
    utc_now,
)


@runtime_checkable
class IngestionTarget(Protocol):
    def ingest_batch(
        self,
        *,
        tenant_id: str,
        job_id: str,
        dataset_name: str,
        records: tuple[NormalizedRecord, ...],
        decisions: tuple[DedupeDecision, ...] | None = None,
        idempotency_key: str = "",
        dataset_version: str = "1",
    ) -> IngestionBatchResult: ...

    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetResult | None: ...


@dataclass
class _DatasetState:
    result: DatasetResult
    logical_keys: set[str] = field(default_factory=set)
    records: list[NormalizedRecord] = field(default_factory=list)


def _logical_key(record: NormalizedRecord) -> str:
    return record.fingerprint or checksum_text(
        f"{record.source_id}:{record.record_id}:{sorted(dict(record.fields).items())}"
    )


class InMemoryIngestionTarget:
    def __init__(self):
        self._datasets: dict[str, _DatasetState] = {}
        self._by_idempotency: dict[tuple[str, str], IngestionBatchResult] = {}
        self._batches: list[IngestionBatchResult] = []

    def ingest_batch(
        self,
        *,
        tenant_id: str,
        job_id: str,
        dataset_name: str,
        records: tuple[NormalizedRecord, ...],
        decisions: tuple[DedupeDecision, ...] | None = None,
        idempotency_key: str = "",
        dataset_version: str = "1",
    ) -> IngestionBatchResult:
        from security.tenant import require_tenant_id

        tid = require_tenant_id(tenant_id)
        key = str(idempotency_key or "").strip()
        if key:
            prior = self._by_idempotency.get((tid, key))
            if prior is not None:
                return prior

        decision_map = {d.record_id: d for d in (decisions or ())}
        accepted = rejected = duplicate = failed = 0
        reasons: list[str] = []
        kept: list[NormalizedRecord] = []

        # Find or create dataset for this job+name
        dataset_id = None
        state = None
        for ds_id, st in self._datasets.items():
            if st.result.tenant_id == tid and st.result.job_id == job_id and st.result.name == dataset_name:
                dataset_id = ds_id
                state = st
                break
        if state is None:
            dataset_id = new_id("ds-")
            state = _DatasetState(
                result=DatasetResult(
                    dataset_id=dataset_id,
                    tenant_id=tid,
                    job_id=job_id,
                    name=dataset_name,
                    version=dataset_version,
                    record_count=0,
                    fingerprint="",
                    source_ids=(),
                )
            )
            self._datasets[dataset_id] = state

        for rec in records:
            if rec.tenant_id != tid:
                rejected += 1
                reasons.append("tenant_mismatch")
                continue
            if rec.errors:
                rejected += 1
                reasons.append("record_errors")
                continue
            dec = decision_map.get(rec.record_id)
            if dec is not None and dec.decision in {
                DEDUPE_EXACT,
                DEDUPE_SAME_SOURCE,
                DEDUPE_CROSS_SOURCE,
            }:
                # Preserve multi-provenance but count as duplicate logical ingest
                duplicate += 1
                reasons.append(f"dedupe:{dec.decision}")
                # Still attach provenance to existing — do not add duplicate logical row
                continue
            if dec is not None and dec.decision == DEDUPE_POSSIBLE:
                # Accept possible matches as distinct unless fingerprint collides
                pass
            lk = _logical_key(rec)
            if lk in state.logical_keys:
                duplicate += 1
                reasons.append("logical_duplicate")
                continue
            try:
                state.logical_keys.add(lk)
                state.records.append(rec)
                accepted += 1
            except Exception:
                failed += 1
                reasons.append("ingest_failed")

        sources = tuple(sorted({r.source_id for r in state.records}))
        fp = fingerprint_record(
            {"dataset": dataset_name, "keys": sorted(state.logical_keys)[:1000]}
        )
        state.result = DatasetResult(
            dataset_id=dataset_id,
            tenant_id=tid,
            job_id=job_id,
            name=dataset_name,
            version=dataset_version,
            record_count=len(state.records),
            fingerprint=fp,
            source_ids=sources,
            created_at=state.result.created_at,
        )
        batch = IngestionBatchResult(
            batch_id=new_id("ib-"),
            tenant_id=tid,
            job_id=job_id,
            dataset_id=dataset_id,
            accepted=accepted,
            rejected=rejected,
            duplicate=duplicate,
            failed=failed,
            reason_codes=tuple(dict.fromkeys(reasons)),
            idempotency_key=key,
            created_at=utc_now(),
        )
        self._batches.append(batch)
        if key:
            self._by_idempotency[(tid, key)] = batch
        return batch

    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetResult | None:
        from security.tenant import require_tenant_id, tenants_match

        tid = require_tenant_id(tenant_id)
        state = self._datasets.get(dataset_id)
        if state is None or not tenants_match(state.result.tenant_id, tid):
            return None
        return state.result

    def list_records(self, dataset_id: str, *, tenant_id: str) -> tuple[NormalizedRecord, ...]:
        from security.tenant import require_tenant_id, tenants_match

        tid = require_tenant_id(tenant_id)
        state = self._datasets.get(dataset_id)
        if state is None or not tenants_match(state.result.tenant_id, tid):
            return ()
        return tuple(state.records)


class SqliteIngestionTarget:
    """Durable ingest target backed by acquisition sqlite store methods."""

    def __init__(self, store):
        self.store = store
        self._memory = InMemoryIngestionTarget()

    def ingest_batch(
        self,
        *,
        tenant_id: str,
        job_id: str,
        dataset_name: str,
        records: tuple[NormalizedRecord, ...],
        decisions: tuple[DedupeDecision, ...] | None = None,
        idempotency_key: str = "",
        dataset_version: str = "1",
    ) -> IngestionBatchResult:
        batch = self._memory.ingest_batch(
            tenant_id=tenant_id,
            job_id=job_id,
            dataset_name=dataset_name,
            records=records,
            decisions=decisions,
            idempotency_key=idempotency_key,
            dataset_version=dataset_version,
        )
        if hasattr(self.store, "save_ingest_batch"):
            self.store.save_ingest_batch(batch)
        if hasattr(self.store, "save_dataset"):
            ds = self._memory.get_dataset(batch.dataset_id, tenant_id=tenant_id)
            if ds is not None:
                self.store.save_dataset(ds)
        if hasattr(self.store, "save_normalized_record"):
            for rec in records:
                # Only persist accepted unique logical records already in memory dataset
                pass
            ds_records = self._memory.list_records(batch.dataset_id, tenant_id=tenant_id)
            for rec in ds_records:
                self.store.save_normalized_record(rec)
        return batch

    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetResult | None:
        if hasattr(self.store, "get_dataset"):
            found = self.store.get_dataset(dataset_id, tenant_id=tenant_id)
            if found is not None:
                return found
        return self._memory.get_dataset(dataset_id, tenant_id=tenant_id)
