"""Content Intelligence Service — governed Content Factory facade."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from content_intel.access import ContentAccessPolicy
from content_intel.analytics import PerformanceAnalytics
from content_intel.competitors import build_competitor_profile, build_trend_signal
from content_intel.errors import (
    CONTENT_MEDIA_UNAVAILABLE,
    CONTENT_PLAN_INVALID,
    ContentBatchRequired,
    ContentIntelError,
    ContentInsufficientEvidence,
)
from content_intel.generator import DeterministicContentGenerator, GenerationContext
from content_intel.observability import ContentObservability
from content_intel.optimization import OptimizationEngine
from content_intel.planner import assert_sync_content_allowed, plan_content_job
from content_intel.platform_models import (
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_SCHEDULED,
    STATUS_VALIDATED,
    BrandProfile,
    ContentAssetVersion,
    ContentExperiment,
    ContentHook,
    ContentIdea,
    ContentJob,
    ContentObjective,
    ContentProject,
    ContentScript,
    ContentStrategy,
    MediaAssetRef,
    MediaBrief,
    PublicationItem,
    PublicationPlan,
    OptimizationDecision,
    ResearchReport,
)
from content_intel.research import build_research_report
from content_intel.store import ContentStore
from content_intel.validation import ContentValidator
from security.tenant import require_tenant_id


class ContentIntelligenceService:
    def __init__(
        self,
        store: ContentStore,
        *,
        access: ContentAccessPolicy | None = None,
        generator: DeterministicContentGenerator | None = None,
        validator: ContentValidator | None = None,
        analytics: PerformanceAnalytics | None = None,
        optimizer: OptimizationEngine | None = None,
        knowledge_service=None,
        tool_gateway=None,
        observability=None,
        product_media_service=None,
    ):
        self.store = store
        self.access = access or ContentAccessPolicy()
        self.generator = generator or DeterministicContentGenerator()
        self.validator = validator or ContentValidator()
        self.analytics = analytics or PerformanceAnalytics()
        self.optimizer = optimizer or OptimizationEngine()
        self.knowledge_service = knowledge_service
        self.tool_gateway = tool_gateway
        self.product_media_service = product_media_service
        self.obs = ContentObservability(observability)

    def create_project(self, *, tenant_id: str, name: str, owner_ref: str = "") -> ContentProject:
        tenant = require_tenant_id(tenant_id)
        project = ContentProject(
            project_id=str(uuid.uuid4()),
            tenant_id=tenant,
            name=name,
            owner_ref=owner_ref,
        )
        self.store.save_project(project)
        return project

    def research(
        self,
        *,
        tenant_id: str,
        project_id: str,
        objective_id: str,
        evidence_rows: list[dict],
        max_evidence: int = 50,
        bulk: bool = False,
    ) -> ResearchReport:
        tenant = require_tenant_id(tenant_id)
        try:
            assert_sync_content_allowed(item_count=len(evidence_rows), bulk=bulk)
        except ContentBatchRequired:
            self.obs.emit("content.failed", status="batch_required", metadata={"stage": "research"})
            raise
        self.obs.emit("content.research.started", status="started", metadata={"project_id": project_id})
        if self.knowledge_service is not None:
            # Governed enrichment path — optional bounded retrieval
            pass
        report = build_research_report(
            tenant_id=tenant,
            project_id=project_id,
            objective_id=objective_id,
            evidence_rows=evidence_rows,
            max_evidence=max_evidence,
        )
        self.store.save_research(report)
        self.obs.emit(
            "content.research.completed",
            status="ok",
            metadata={"report_id": report.report_id, "count": len(report.evidence)},
        )
        return report

    def get_research(self, report_id: str, *, tenant_id: str) -> ResearchReport | None:
        tenant = require_tenant_id(tenant_id)
        report = self.store.get_research(report_id, tenant_id=tenant)
        if report is None:
            return None
        self.access.require(requesting_tenant=tenant, target_tenant=report.tenant_id)
        return report

    def analyze_competitors(
        self,
        *,
        tenant_id: str,
        competitors: list[dict],
        evidence_refs: tuple[str, ...] = (),
    ) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        self.obs.emit("content.competitor_analysis.completed", status="started", metadata={})
        profiles = [
            build_competitor_profile(
                tenant_id=tenant,
                name=str(c.get("name") or "competitor"),
                category=str(c.get("category") or "general"),
                observations=list(c.get("observations") or []),
                evidence_refs=evidence_refs,
            )
            for c in competitors
        ]
        self.obs.emit(
            "content.competitor_analysis.completed",
            status="ok",
            metadata={"count": len(profiles)},
        )
        return [{"competitor_id": p.competitor_id, "name": p.name} for p in profiles]

    def analyze_trends(
        self,
        *,
        tenant_id: str,
        topic: str,
        counts: list[float],
        evidence_count: int,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        signal = build_trend_signal(
            tenant_id=tenant,
            topic=topic,
            counts=counts,
            evidence_count=evidence_count,
        )
        self.store.save_trend(signal)
        self.obs.emit("content.trend_analysis.completed", status="ok", metadata={"trend_id": signal.trend_id})
        return {
            "trend_id": signal.trend_id,
            "magnitude": signal.magnitude,
            "velocity": signal.velocity,
            "status": signal.status,
        }

    def create_strategy(
        self,
        *,
        tenant_id: str,
        project_id: str,
        objective: str,
        channel: str,
        audience_segments: tuple[str, ...],
        evidence_refs: tuple[str, ...] = (),
        brand: BrandProfile | None = None,
        parent_version_id: str | None = None,
        version_num: int = 1,
    ) -> ContentStrategy:
        tenant = require_tenant_id(tenant_id)
        ctx = GenerationContext(
            tenant_id=tenant,
            project_id=project_id,
            channel=channel,
            objective=objective,
            audience_segments=audience_segments,
            pillars=("trust", "value"),
            evidence_refs=evidence_refs,
            brand_tone=brand.tone if brand else "professional",
        )
        strategy = self.generator.generate_strategy(ctx)
        if parent_version_id:
            strategy = ContentStrategy(
                strategy_id=strategy.strategy_id,
                version_id=str(uuid.uuid4()),
                tenant_id=tenant,
                project_id=project_id,
                version_num=version_num,
                pillars=strategy.pillars,
                channel_roles=strategy.channel_roles,
                messaging_principles=strategy.messaging_principles,
                evidence_refs=strategy.evidence_refs,
                parent_version_id=parent_version_id,
            )
        self.store.save_strategy(strategy)
        self.obs.emit("content.strategy.created", status="ok", metadata={"version_id": strategy.version_id})
        return strategy

    def generate_ideas(
        self,
        *,
        tenant_id: str,
        project_id: str,
        objective: str,
        channel: str,
        count: int = 3,
        evidence_refs: tuple[str, ...] = (),
    ) -> tuple[ContentIdea, ...]:
        tenant = require_tenant_id(tenant_id)
        ctx = GenerationContext(
            tenant_id=tenant,
            project_id=project_id,
            channel=channel,
            objective=objective,
            audience_segments=("general",),
            pillars=(),
            evidence_refs=evidence_refs,
        )
        ideas = self.generator.generate_ideas(ctx, count=count)
        seen: set[str] = set()
        unique: list[ContentIdea] = []
        for idea in ideas:
            key = self.generator.dedupe_key(idea.concept)
            if key in seen:
                continue
            seen.add(key)
            self.store.save_idea(idea)
            unique.append(idea)
        self.obs.emit("content.idea.created", status="ok", metadata={"count": len(unique)})
        return tuple(unique)

    def generate_hook(self, idea: ContentIdea, *, tenant_id: str) -> ContentHook:
        self.access.require(requesting_tenant=tenant_id, target_tenant=idea.tenant_id)
        hook = self.generator.generate_hook(idea)
        self.store.save_hook(hook)
        return hook

    def generate_script(self, idea: ContentIdea, hook: ContentHook, *, tenant_id: str) -> ContentScript:
        self.access.require(requesting_tenant=tenant_id, target_tenant=idea.tenant_id)
        script = self.generator.generate_script(idea, hook)
        self.store.save_script(script)
        self.obs.emit("content.script.created", status="ok", metadata={"version_id": script.version_id})
        return script

    def generate_copy(
        self,
        *,
        tenant_id: str,
        project_id: str,
        content_type: str,
        channel: str,
        objective: str,
        product_facts: dict[str, str] | None = None,
        brand: BrandProfile | None = None,
        strategy_version_id: str | None = None,
        idea_id: str | None = None,
        bulk: bool = False,
    ) -> ContentAssetVersion:
        tenant = require_tenant_id(tenant_id)
        if not bulk:
            assert_sync_content_allowed(item_count=1, bulk=False)
        ctx = GenerationContext(
            tenant_id=tenant,
            project_id=project_id,
            channel=channel,
            objective=objective,
            audience_segments=("general",),
            pillars=(),
            evidence_refs=(),
            brand_tone=brand.tone if brand else "professional",
            product_facts=product_facts,
            forbidden_terms=brand.forbidden_terms if brand else (),
        )
        asset = self.generator.generate_copy(
            ctx,
            content_type=content_type,
            strategy_version_id=strategy_version_id,
            idea_id=idea_id,
        )
        asset = self.validator.validate_asset(asset, brand=brand)
        self.store.save_asset(asset)
        self.obs.emit(
            "content.asset.generated",
            status=asset.status,
            metadata={"version_id": asset.version_id, "content_type": content_type},
        )
        if asset.status == STATUS_VALIDATED:
            self.obs.emit("content.asset.validated", status="ok", metadata={"version_id": asset.version_id})
        return asset

    def bulk_generate_product_content(
        self,
        *,
        tenant_id: str,
        project_id: str,
        products: list[dict],
        channel: str = "marketplace",
        bulk: bool = True,
        job_id: str | None = None,
        checkpoint: int = 0,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        planned = plan_content_job(
            tenant_id=tenant,
            project_id=project_id,
            item_count=len(products),
            bulk=bulk,
        )
        if planned.enqueue and not bulk:
            raise ContentBatchRequired()

        jid = job_id or str(uuid.uuid4())
        job = ContentJob(
            job_id=jid,
            tenant_id=tenant,
            project_id=project_id,
            stage="GENERATE",
            status="running",
            checkpoint=checkpoint,
            total=len(products),
        )
        self.store.save_job(job)

        generated: list[str] = []
        failed = 0
        start = checkpoint
        for idx, product in enumerate(products[start:], start=start):
            try:
                asset = self.generate_copy(
                    tenant_id=tenant,
                    project_id=project_id,
                    content_type="product_description",
                    channel=channel,
                    objective=str(product.get("name") or "product"),
                    product_facts={
                        k: str(v)
                        for k, v in product.items()
                        if k in {"sku", "name", "price", "stock", "warranty"}
                    },
                    bulk=True,
                )
                generated.append(asset.version_id)
            except ContentIntelError:
                failed += 1
            self.store.save_job(
                ContentJob(
                    job_id=jid,
                    tenant_id=tenant,
                    project_id=project_id,
                    stage="GENERATE",
                    status="running",
                    checkpoint=idx + 1,
                    total=len(products),
                )
            )

        status = "completed" if failed == 0 else "partial"
        self.store.save_job(
            ContentJob(
                job_id=jid,
                tenant_id=tenant,
                project_id=project_id,
                stage="GENERATE",
                status=status,
                checkpoint=len(products),
                total=len(products),
            )
        )
        return {
            "job_id": jid,
            "generated": len(generated),
            "failed": failed,
            "version_ids": generated,
            "trusted_metadata": dict(planned.trusted_metadata),
        }

    def create_media_brief(
        self,
        *,
        tenant_id: str,
        asset_version_id: str,
        media_type: str,
        aspect_ratio: str,
        scene_description: str,
    ) -> MediaBrief:
        tenant = require_tenant_id(tenant_id)
        asset = self.store.get_asset(asset_version_id, tenant_id=tenant)
        if asset is None:
            raise ContentIntelError(CONTENT_PLAN_INVALID)
        brief = MediaBrief(
            brief_id=str(uuid.uuid4()),
            tenant_id=tenant,
            asset_version_id=asset_version_id,
            media_type=media_type,
            aspect_ratio=aspect_ratio,
            scene_description=scene_description,
        )
        self.store.save_media_brief(brief)
        self.obs.emit("content.media.requested", status="requested", metadata={"brief_id": brief.brief_id})
        return brief

    def generate_media(
        self,
        *,
        tenant_id: str,
        brief: MediaBrief,
        provider_id: str = "fake_media",
    ) -> MediaAssetRef:
        tenant = require_tenant_id(tenant_id)
        self.access.require(requesting_tenant=tenant, target_tenant=brief.tenant_id)
        if self.product_media_service is None and self.tool_gateway is None and provider_id != "fake_media":
            raise ContentIntelError(CONTENT_MEDIA_UNAVAILABLE)
        artifact_id = str(uuid.uuid4())
        content_hash = artifact_id
        if self.product_media_service is not None:
            result = self.product_media_service.generate_from_brief(
                tenant_id=tenant,
                scene_description=brief.scene_description,
                aspect_ratio=brief.aspect_ratio,
                variant_count=1,
                media_brief_id=brief.brief_id,
            )
            version_ids = result.get("version_ids") or []
            if version_ids:
                version = self.product_media_service.get(tenant_id=tenant, version_id=version_ids[0])
                if version is not None:
                    artifact_id = version.version_id
                    content_hash = version.content_hash
        ref = MediaAssetRef(
            ref_id=str(uuid.uuid4()),
            tenant_id=tenant,
            brief_id=brief.brief_id,
            artifact_id=artifact_id,
            provider_id=provider_id if self.product_media_service is None else "product_media",
            status=STATUS_APPROVED,
            content_hash=content_hash,
        )
        self.store.save_media_ref(ref)
        self.obs.emit("content.media.generated", status="ok", metadata={"ref_id": ref.ref_id, "artifact_id": artifact_id})
        return ref

    def create_publication_plan(
        self,
        *,
        tenant_id: str,
        project_id: str,
        items: list[dict],
        version_num: int = 1,
        parent_version_id: str | None = None,
    ) -> PublicationPlan:
        tenant = require_tenant_id(tenant_id)
        pub_items: list[PublicationItem] = []
        seen_assets: set[str] = set()
        for row in items:
            asset_vid = str(row["asset_version_id"])
            asset = self.store.get_asset(asset_vid, tenant_id=tenant)
            if asset is None:
                raise ContentIntelError(CONTENT_PLAN_INVALID)
            if asset.status not in {STATUS_VALIDATED, STATUS_APPROVED}:
                raise ContentIntelError(CONTENT_PLAN_INVALID, "asset_not_validated")
            if asset_vid in seen_assets:
                raise ContentIntelError(CONTENT_PLAN_INVALID, "duplicate_schedule")
            seen_assets.add(asset_vid)
            if row.get("require_media") and not row.get("media_ref"):
                raise ContentIntelError(CONTENT_PLAN_INVALID, "missing_media")
            scheduled = row.get("scheduled_at") or datetime.now(timezone.utc)
            tz = str(row.get("timezone") or "UTC")
            pub_items.append(
                PublicationItem(
                    item_id=str(uuid.uuid4()),
                    asset_version_id=asset_vid,
                    channel=str(row.get("channel") or asset.channel),
                    scheduled_at=scheduled,
                    timezone=tz,
                    status=STATUS_SCHEDULED,
                )
            )
        plan = PublicationPlan(
            plan_id=str(uuid.uuid4()),
            version_id=str(uuid.uuid4()),
            tenant_id=tenant,
            project_id=project_id,
            version_num=version_num,
            items=tuple(pub_items),
            parent_version_id=parent_version_id,
        )
        self.store.save_plan(plan)
        self.obs.emit("content.plan.created", status="ok", metadata={"version_id": plan.version_id})
        return plan

    def ingest_performance(
        self,
        *,
        tenant_id: str,
        project_id: str,
        rows: list[dict],
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        for row in rows:
            asset = self.store.get_asset(str(row["asset_version_id"]), tenant_id=tenant)
            if asset is None:
                raise ContentIntelError(CONTENT_PLAN_INVALID, "foreign_asset")
        observations = self.analytics.ingest_observations(rows, tenant_id=tenant)
        for obs in observations:
            self.store.save_observation(obs)
        report = self.analytics.analyze(observations, tenant_id=tenant, project_id=project_id)
        self.store.save_report(report)
        self.obs.emit("content.performance.ingested", status="ok", metadata={"count": len(observations)})
        self.obs.emit("content.performance.analyzed", status="ok", metadata={"report_id": report.report_id})
        return {
            "report_id": report.report_id,
            "metrics": dict(report.metrics_computed),
            "limitations": list(report.limitations),
        }

    def optimize(
        self,
        *,
        tenant_id: str,
        project_id: str,
        strategy_version_id: str,
        asset_version_ids: tuple[str, ...],
        observation_window: tuple[datetime, datetime],
        metrics: dict,
    ) -> OptimizationDecision:
        tenant = require_tenant_id(tenant_id)
        self.obs.emit("content.optimization.started", status="started", metadata={})
        for vid in asset_version_ids:
            asset = self.store.get_asset(vid, tenant_id=tenant)
            if asset is None:
                raise ContentIntelError(CONTENT_PLAN_INVALID, "foreign_asset_version")
        existing = self.optimizer.decide(
            tenant_id=tenant,
            project_id=project_id,
            strategy_version_id=strategy_version_id,
            asset_version_ids=asset_version_ids,
            observation_window=observation_window,
            metrics=metrics,
        )
        prior = self.store.get_optimization_by_key(
            tenant_id=tenant, idempotency_key=existing.idempotency_key
        )
        if prior is not None:
            return prior
        self.store.save_optimization(existing)
        self.obs.emit(
            "content.optimization.completed",
            status="ok",
            metadata={"decision_id": existing.decision_id},
        )
        return existing

    def plan_job(self, *, tenant_id: str, project_id: str, item_count: int, bulk: bool = False) -> dict:
        planned = plan_content_job(
            tenant_id=tenant_id,
            project_id=project_id,
            item_count=item_count,
            bulk=bulk,
        )
        return {
            "enqueue": planned.enqueue,
            "execution_lane": planned.execution_lane,
            "trusted_metadata": dict(planned.trusted_metadata),
        }

    def get_asset(self, version_id: str, *, tenant_id: str) -> ContentAssetVersion | None:
        tenant = require_tenant_id(tenant_id)
        asset = self.store.get_asset(version_id, tenant_id=tenant)
        if asset is None:
            return None
        self.access.require(requesting_tenant=tenant, target_tenant=asset.tenant_id)
        return asset

    def create_experiment(
        self,
        *,
        tenant_id: str,
        hypothesis: str,
        variant_version_ids: tuple[str, ...],
        target_metric: str = "ctr",
    ) -> ContentExperiment:
        tenant = require_tenant_id(tenant_id)
        for vid in variant_version_ids:
            asset = self.store.get_asset(vid, tenant_id=tenant)
            if asset is None:
                raise ContentIntelError(CONTENT_PLAN_INVALID, "foreign_asset_version")
            self.access.require(requesting_tenant=tenant, target_tenant=asset.tenant_id)
        exp = ContentExperiment(
            experiment_id=str(uuid.uuid4()),
            tenant_id=tenant,
            hypothesis=hypothesis,
            variant_version_ids=variant_version_ids,
            target_metric=target_metric,
            outcome="inconclusive",
        )
        self.store.save_experiment(exp)
        self.obs.emit("content.experiment.created", status="ok", metadata={"experiment_id": exp.experiment_id})
        return exp
