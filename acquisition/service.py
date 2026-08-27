"""AcquisitionService — public internal API for the Data Acquisition Platform."""

from __future__ import annotations

from acquisition.change import detect_record_change
from acquisition.crawler import ControlledCrawler, CrawlLimits, CrawlResult
from acquisition.entity import resolve_entities
from acquisition.errors import AcquisitionDeniedError, InvalidRecordError
from acquisition.freshness import freshness_label
from acquisition.manager import AcquisitionManager
from acquisition.models import (
    AcquisitionRequest,
    ChangeEvent,
    EntityMatchResult,
    ParsedRecord,
    RawArtifact,
    SourceDescriptor,
)
from acquisition.parsers import ParserRegistry, build_default_parser_registry
from acquisition.registry import SourceRegistry
from acquisition.schedule import AcquisitionScheduler
from acquisition.store import AcquisitionStore, InMemoryAcquisitionStore
from acquisition.trust import trust_weight
from security.tenant import normalize_tenant_id


class AcquisitionService:
    def __init__(
        self,
        *,
        source_registry: SourceRegistry | None = None,
        store: AcquisitionStore | None = None,
        parser_registry: ParserRegistry | None = None,
        tool_gateway=None,
        scheduler: AcquisitionScheduler | None = None,
    ):
        self.sources = source_registry or SourceRegistry()
        self.store = store or InMemoryAcquisitionStore()
        self.parsers = parser_registry or build_default_parser_registry()
        self.gateway = tool_gateway
        self.manager = AcquisitionManager(
            source_registry=self.sources,
            tool_gateway=tool_gateway,
            store=self.store,
        )
        self.crawler = ControlledCrawler(self.manager)
        self.scheduler = scheduler or AcquisitionScheduler()
        self.last_metrics: dict = {}

    # --- Sources ---
    def register_source(self, descriptor: SourceDescriptor) -> SourceDescriptor:
        self.sources.register(descriptor)
        self.store.save_source(descriptor)
        return descriptor

    def list_sources(self, *, tenant_id: str, include_disabled: bool = False):
        return self.sources.list_sources(tenant_id=tenant_id, include_disabled=include_disabled)

    def get_source(self, source_id: str, *, tenant_id: str) -> SourceDescriptor:
        return self.sources.get(source_id, tenant_id=tenant_id)

    def set_source_enabled(self, source_id: str, *, tenant_id: str, enabled: bool) -> SourceDescriptor:
        desc = self.sources.enable(source_id, tenant_id=tenant_id, enabled=enabled)
        self.store.save_source(desc)
        return desc

    # --- Acquisition ---
    async def acquire(self, request: AcquisitionRequest) -> RawArtifact:
        return await self.manager.acquire(request)

    async def crawl(
        self,
        *,
        source_id: str,
        tenant_id: str,
        seeds: tuple[str, ...],
        workflow_id: str = "",
        max_depth: int = 1,
        max_pages: int = 10,
    ) -> CrawlResult:
        source = self.sources.get(source_id, tenant_id=tenant_id)
        return await self.crawler.crawl(
            source=source,
            seeds=seeds,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            limits=CrawlLimits(max_depth=max_depth, max_pages=max_pages),
        )

    def ingest_text(
        self,
        *,
        source_id: str,
        tenant_id: str,
        text: str,
        content_type: str,
        url: str = "",
        metadata: dict | None = None,
    ) -> RawArtifact:
        source = self.sources.get(source_id, tenant_id=tenant_id)
        return self.manager.ingest_text_artifact(
            source=source,
            tenant_id=tenant_id,
            text=text,
            content_type=content_type,
            url=url,
            metadata=metadata,
        )

    # --- Parse ---
    def parse_artifact(self, artifact_id: str, *, tenant_id: str) -> tuple[ParsedRecord, ...]:
        artifact = self.store.get_artifact(artifact_id, tenant_id=tenant_id)
        if artifact is None:
            raise AcquisitionDeniedError("artifact_not_found")
        return self.parse(artifact)

    def parse(self, artifact: RawArtifact) -> tuple[ParsedRecord, ...]:
        records = self.parsers.parse(artifact)
        stored = []
        changes = []
        for rec in records:
            # Enrich confidence with source trust weight (evidence only)
            try:
                source = self.sources.get(rec.source_id, tenant_id=rec.tenant_id)
                weight = trust_weight(source.trust_level)
                from dataclasses import replace

                rec = replace(
                    rec,
                    confidence=min(1.0, float(rec.confidence) * (0.5 + 0.5 * weight)),
                    freshness=freshness_label(
                        fetched_at=artifact.fetched_at,
                        policy=source.freshness_policy,
                    ),
                )
            except Exception:
                pass

            # Natural key for change detection
            fields = dict(rec.fields)
            natural = str(
                fields.get("ean")
                or fields.get("sku")
                or fields.get("supplier_sku")
                or fields.get("mpn")
                or ""
            )
            previous = None
            if natural and hasattr(self.store, "find_previous_observation"):
                previous = self.store.find_previous_observation(
                    tenant_id=rec.tenant_id,
                    source_id=rec.source_id,
                    natural_key=natural,
                )
            else:
                previous = self.store.find_record_by_fingerprint(
                    rec.fingerprint, tenant_id=rec.tenant_id, source_id=rec.source_id
                )

            # Dedupe identical fingerprint
            existing = self.store.find_record_by_fingerprint(
                rec.fingerprint, tenant_id=rec.tenant_id, source_id=rec.source_id
            )
            if existing is not None:
                stored.append(existing)
                continue

            saved = self.store.save_record(rec)
            event = detect_record_change(previous=previous, current=saved)
            self.store.save_change(event)
            changes.append(event)
            stored.append(saved)

        self.last_metrics = {
            "records_produced": len(stored),
            "changes": len(changes),
            "artifact_id": artifact.artifact_id,
            "source_id": artifact.source_id,
            "tenant_id": normalize_tenant_id(artifact.tenant_id),
        }
        return tuple(stored)

    # --- Query ---
    def list_records(self, *, tenant_id: str, source_id: str | None = None, record_type: str | None = None):
        return self.store.list_records(
            tenant_id=tenant_id, source_id=source_id, record_type=record_type
        )

    def get_record(self, record_id: str, *, tenant_id: str) -> ParsedRecord | None:
        return self.store.get_record(record_id, tenant_id=tenant_id)

    def list_changes(self, *, tenant_id: str, source_id: str | None = None, record_id: str | None = None):
        return self.store.list_changes(
            tenant_id=tenant_id, source_id=source_id, record_id=record_id
        )

    def get_provenance(self, record_id: str, *, tenant_id: str) -> dict:
        rec = self.store.get_record(record_id, tenant_id=tenant_id)
        if rec is None:
            raise AcquisitionDeniedError("record_not_found")
        art = self.store.get_artifact(rec.artifact_id, tenant_id=tenant_id)
        source = None
        try:
            source = self.sources.get(rec.source_id, tenant_id=tenant_id)
        except Exception:
            source = self.store.get_source(rec.source_id, tenant_id=tenant_id)
        return {
            "record_id": rec.record_id,
            "record_provenance": dict(rec.provenance),
            "artifact": {
                "artifact_id": art.artifact_id if art else rec.artifact_id,
                "checksum": art.checksum if art else None,
                "url": art.url if art else None,
                "fetched_at": art.fetched_at.isoformat() if art else None,
                "content_trust": art.content_trust if art else rec.content_trust,
            },
            "source": {
                "source_id": rec.source_id,
                "trust_level": source.trust_level if source else None,
                "source_type": source.source_type if source else None,
            },
            "parser": {"parser_id": rec.parser_id, "version": rec.parser_version},
            "freshness": rec.freshness,
            "fingerprint": rec.fingerprint,
        }

    def resolve_entity(self, left_id: str, right_id: str, *, tenant_id: str) -> EntityMatchResult:
        left = self.store.get_record(left_id, tenant_id=tenant_id)
        right = self.store.get_record(right_id, tenant_id=tenant_id)
        if left is None or right is None:
            raise AcquisitionDeniedError("record_not_found")
        return resolve_entities(left, right)

    # --- Schedule ---
    def schedule_refresh(
        self,
        *,
        schedule_id: str,
        source_id: str,
        tenant_id: str,
        interval_seconds: float,
        target: str = "",
        acquisition_type: str = "http_get",
    ):
        # Ensure source belongs to tenant
        self.sources.get(source_id, tenant_id=tenant_id)
        return self.scheduler.register_source_refresh(
            schedule_id=schedule_id,
            source_id=source_id,
            tenant_id=tenant_id,
            interval_seconds=interval_seconds,
            target=target,
            acquisition_type=acquisition_type,
        )
