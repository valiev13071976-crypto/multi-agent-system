"""In-memory tenant-scoped structured acquisition store."""

from __future__ import annotations

from acquisition.models import ChangeEvent, ParsedRecord, RawArtifact, SourceDescriptor
from security.tenant import normalize_tenant_id, tenants_match


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
