"""Multi-layer dedupe — URL/resource, raw hash, structured fingerprint, composite key."""

from __future__ import annotations

from dataclasses import dataclass, field

from acquisition.models import (
    DEDUPE_CROSS_SOURCE,
    DEDUPE_EXACT,
    DEDUPE_POLICY_VERSION,
    DEDUPE_POSSIBLE,
    DEDUPE_SAME_SOURCE,
    DEDUPE_UNIQUE,
    DedupeDecision,
    NormalizedRecord,
    new_id,
    utc_now,
)


@dataclass
class DedupeIndex:
    """In-memory / store-backed index preserving multi-provenance."""

    by_url: dict[tuple[str, str], str] = field(default_factory=dict)  # (tenant, url) -> record_id
    by_raw_hash: dict[tuple[str, str], str] = field(default_factory=dict)
    by_fingerprint: dict[tuple[str, str], str] = field(default_factory=dict)
    by_composite: dict[tuple[str, str], str] = field(default_factory=dict)
    provenance: dict[str, list[str]] = field(default_factory=dict)  # record_id -> provenance refs
    records: dict[str, NormalizedRecord] = field(default_factory=dict)


def composite_key(record: NormalizedRecord) -> str:
    fields = dict(record.fields)
    parts = [
        str(fields.get("ean") or ""),
        str(fields.get("sku") or fields.get("supplier_sku") or ""),
        str(fields.get("mpn") or ""),
        str(fields.get("brand") or "").lower(),
    ]
    return "|".join(parts)


class DedupeEngine:
    version: str = DEDUPE_POLICY_VERSION

    def __init__(self, index: DedupeIndex | None = None):
        self.index = index or DedupeIndex()

    def decide(
        self,
        record: NormalizedRecord,
        *,
        job_id: str,
        url: str = "",
        raw_hash: str = "",
    ) -> DedupeDecision:
        tid = record.tenant_id
        prov_ref = f"{record.source_id}:{record.resource_id}:{record.record_id}"

        # Layer 1: URL/resource
        if url:
            key = (tid, url)
            existing_id = self.index.by_url.get(key)
            if existing_id and existing_id in self.index.records:
                existing = self.index.records[existing_id]
                self.index.provenance.setdefault(existing_id, []).append(prov_ref)
                decision = (
                    DEDUPE_SAME_SOURCE
                    if existing.source_id == record.source_id
                    else DEDUPE_CROSS_SOURCE
                )
                return DedupeDecision(
                    decision_id=new_id("dd-"),
                    tenant_id=tid,
                    job_id=job_id,
                    record_id=record.record_id,
                    decision=decision,
                    matched_record_id=existing_id,
                    layer="url",
                    policy_version=self.version,
                    provenance_refs=tuple(self.index.provenance.get(existing_id, [])),
                    created_at=utc_now(),
                )

        # Layer 2: raw content hash
        if raw_hash:
            key = (tid, raw_hash)
            existing_id = self.index.by_raw_hash.get(key)
            if existing_id and existing_id in self.index.records:
                existing = self.index.records[existing_id]
                self.index.provenance.setdefault(existing_id, []).append(prov_ref)
                decision = (
                    DEDUPE_EXACT
                    if existing.source_id == record.source_id
                    else DEDUPE_CROSS_SOURCE
                )
                return DedupeDecision(
                    decision_id=new_id("dd-"),
                    tenant_id=tid,
                    job_id=job_id,
                    record_id=record.record_id,
                    decision=decision,
                    matched_record_id=existing_id,
                    layer="raw_hash",
                    policy_version=self.version,
                    provenance_refs=tuple(self.index.provenance.get(existing_id, [])),
                    created_at=utc_now(),
                )

        # Layer 3: structured fingerprint
        fp_key = (tid, record.fingerprint)
        existing_id = self.index.by_fingerprint.get(fp_key)
        if existing_id and existing_id in self.index.records:
            existing = self.index.records[existing_id]
            self.index.provenance.setdefault(existing_id, []).append(prov_ref)
            decision = (
                DEDUPE_EXACT
                if existing.source_id == record.source_id
                else DEDUPE_CROSS_SOURCE
            )
            return DedupeDecision(
                decision_id=new_id("dd-"),
                tenant_id=tid,
                job_id=job_id,
                record_id=record.record_id,
                decision=decision,
                matched_record_id=existing_id,
                layer="fingerprint",
                policy_version=self.version,
                provenance_refs=tuple(self.index.provenance.get(existing_id, [])),
                created_at=utc_now(),
            )

        # Layer 4: composite key (possible match across sources)
        ck = composite_key(record)
        if ck and ck != "|||":
            ck_key = (tid, ck)
            existing_id = self.index.by_composite.get(ck_key)
            if existing_id and existing_id in self.index.records:
                existing = self.index.records[existing_id]
                self.index.provenance.setdefault(existing_id, []).append(prov_ref)
                if existing.source_id == record.source_id:
                    decision = DEDUPE_SAME_SOURCE
                else:
                    decision = DEDUPE_POSSIBLE
                return DedupeDecision(
                    decision_id=new_id("dd-"),
                    tenant_id=tid,
                    job_id=job_id,
                    record_id=record.record_id,
                    decision=decision,
                    matched_record_id=existing_id,
                    layer="composite",
                    policy_version=self.version,
                    provenance_refs=tuple(self.index.provenance.get(existing_id, [])),
                    created_at=utc_now(),
                )

        # Unique — index it
        self.index.records[record.record_id] = record
        self.index.provenance[record.record_id] = [prov_ref]
        if url:
            self.index.by_url[(tid, url)] = record.record_id
        if raw_hash:
            self.index.by_raw_hash[(tid, raw_hash)] = record.record_id
        self.index.by_fingerprint[(tid, record.fingerprint)] = record.record_id
        if ck and ck != "|||":
            self.index.by_composite[(tid, ck)] = record.record_id

        return DedupeDecision(
            decision_id=new_id("dd-"),
            tenant_id=tid,
            job_id=job_id,
            record_id=record.record_id,
            decision=DEDUPE_UNIQUE,
            layer="none",
            policy_version=self.version,
            provenance_refs=(prov_ref,),
            created_at=utc_now(),
        )
