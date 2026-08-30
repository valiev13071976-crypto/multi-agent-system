"""SEO & Digital Marketing Service — Block 12 facade."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from seo_marketing.access import SeoAccessPolicy, normalize_url
from seo_marketing.analytics import AnalyticsService, compute_conversion_rate, compute_ctr
from seo_marketing.capabilities import (
    CAP_SEO_ANALYTICS_READ,
    CAP_SEO_KEYWORD_ANALYZE,
    CAP_SEO_META_APPLY,
    CAP_SEO_META_GENERATE,
    CAP_SEO_OPTIMIZATION_PLAN,
    CAP_SEO_PERFORMANCE_READ,
    CAP_SEO_SEARCH_CONSOLE_READ,
    CAP_SEO_TECHNICAL_READ,
)
from seo_marketing.errors import (
    SEO_ACCESS_DENIED,
    SEO_PAGE_NOT_FOUND,
    SEO_SITE_NOT_FOUND,
    SEO_STALE_RECOMMENDATION,
    SEO_VALIDATION_FAILED,
    SeoBatchRequired,
    SeoMarketingError,
)
from seo_marketing.keywords import (
    cluster_keywords,
    detect_cannibalization,
    keyword_metrics_from_row,
    map_keyword_to_pages,
    normalize_keywords,
    score_opportunity,
)
from seo_marketing.metadata import capture_meta_snapshot, generate_meta_recommendation, validate_meta
from seo_marketing.observability import SeoObservability
from seo_marketing.optimization import create_optimization_plan, decide_optimization, measure_action
from seo_marketing.performance import METRIC_CLS, METRIC_INP, METRIC_LCP, audit_performance, build_performance_observation
from seo_marketing.planner import assert_sync_seo_allowed
from seo_marketing.platform_models import (
    META_STATUS_APPLIED,
    META_STATUS_VALIDATED,
    SeoJob,
    SeoPage,
    SeoSite,
)
from seo_marketing.policy import BULK_KEYWORD_BATCH_SIZE, BULK_META_APPLY_BATCH_SIZE, BULK_URL_BATCH_SIZE
from seo_marketing.providers.fake_performance import FakePerformanceProvider
from seo_marketing.search_console import SearchConsoleService
from seo_marketing.store import SeoStore
from seo_marketing.technical import analyze_technical_snapshot
from security.tenant import require_tenant_id


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class SeoMarketingService:
    def __init__(
        self,
        store: SeoStore,
        *,
        access: SeoAccessPolicy | None = None,
        search_console: SearchConsoleService | None = None,
        analytics: AnalyticsService | None = None,
        performance_provider=None,
        content_intelligence_service=None,
        product_platform_service=None,
        acquisition_service=None,
        product_media_service=None,
        observability=None,
    ):
        self.store = store
        self.access = access or SeoAccessPolicy()
        self.search_console = search_console or SearchConsoleService()
        self.analytics = analytics or AnalyticsService()
        self.performance = performance_provider or FakePerformanceProvider()
        self.content_intel = content_intelligence_service
        self.product_platform = product_platform_service
        self.acquisition = acquisition_service
        self.product_media = product_media_service
        self.obs = SeoObservability(observability)
        self._applied_meta: set[str] = set()

    def _require_cap(self, capabilities: tuple[str, ...], required: str) -> None:
        if required not in capabilities:
            raise SeoMarketingError(SEO_ACCESS_DENIED)

    def register_site(
        self,
        *,
        tenant_id: str,
        domain: str,
        search_console_property: str = "",
        analytics_property: str = "",
        cms_binding: str = "",
    ) -> SeoSite:
        tenant = require_tenant_id(tenant_id)
        url = normalize_url(domain)
        site = SeoSite(
            site_id=str(uuid.uuid4()),
            tenant_id=tenant,
            domain=url,
            search_console_property=search_console_property,
            analytics_property=analytics_property,
            cms_binding=cms_binding,
        )
        self.store.save_site(site)
        return site

    def get_site(self, site_id: str, *, tenant_id: str) -> SeoSite | None:
        tenant = require_tenant_id(tenant_id)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            return None
        self.access.require(requesting_tenant=tenant, target_tenant=site.tenant_id)
        return site

    def register_page(
        self,
        *,
        tenant_id: str,
        site_id: str,
        url: str,
        product_id: str = "",
    ) -> SeoPage:
        tenant = require_tenant_id(tenant_id)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        page = SeoPage(
            page_id=str(uuid.uuid4()),
            tenant_id=tenant,
            site_id=site_id,
            url=normalize_url(url),
            canonical_url=normalize_url(url),
            product_id=product_id,
        )
        self.store.save_page(page)
        return page

    def get_page(self, page_id: str, *, tenant_id: str) -> SeoPage | None:
        tenant = require_tenant_id(tenant_id)
        page = self.store.get_page(page_id, tenant_id=tenant)
        if page is None:
            return None
        self.access.require(requesting_tenant=tenant, target_tenant=page.tenant_id)
        return page

    def keyword_research(
        self,
        *,
        tenant_id: str,
        site_id: str,
        seeds: list[dict],
        source: str = "seed",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_KEYWORD_ANALYZE)
        assert_sync_seo_allowed(keyword_count=len(seeds), bulk=bulk)
        self.obs.emit("seo.keyword.started", metadata={"site_id": site_id, "count": len(seeds)})
        keywords = normalize_keywords(seeds, tenant_id=tenant, site_id=site_id, source=source)
        for kw in keywords:
            self.store.save_keyword(kw)
        self.obs.emit("seo.keyword.completed", metadata={"count": len(keywords)})
        return {"keyword_ids": [k.keyword_id for k in keywords], "count": len(keywords)}

    def keyword_cluster(
        self,
        *,
        tenant_id: str,
        site_id: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_KEYWORD_ANALYZE)
        keywords = self.store.list_keywords(tenant_id=tenant, site_id=site_id)
        clusters = cluster_keywords(keywords, tenant_id=tenant, site_id=site_id)
        return {"clusters": [c.cluster_id for c in clusters], "count": len(clusters)}

    def keyword_opportunities(
        self,
        *,
        tenant_id: str,
        site_id: str,
        metric_rows: list[dict] | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_KEYWORD_ANALYZE)
        keywords = self.store.list_keywords(tenant_id=tenant, site_id=site_id)
        pages = [{"page_id": p.page_id, "url": p.url, "title": p.url} for p in self.store.list_pages(tenant_id=tenant, site_id=site_id)]
        mappings = [map_keyword_to_pages(k, pages) for k in keywords]
        opportunities = []
        for kw in keywords:
            row = next((r for r in (metric_rows or []) if r.get("query") == kw.text or r.get("keyword_id") == kw.keyword_id), {})
            metrics = keyword_metrics_from_row(kw.keyword_id, row, source="search_console")
            opportunities.append(score_opportunity(kw, metrics=metrics, page_mappings=mappings))
        cannibal = detect_cannibalization(mappings)
        return {
            "opportunities": [{"opportunity_id": o.opportunity_id, "score": o.score, "components": list(o.components)} for o in opportunities],
            "cannibalization": cannibal,
        }

    def meta_inspect(
        self,
        *,
        tenant_id: str,
        page_id: str,
        title: str,
        description: str,
        canonical: str = "",
        robots: str = "",
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        page = self.store.get_page(page_id, tenant_id=tenant)
        if page is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        snap = capture_meta_snapshot(
            tenant_id=tenant,
            page_id=page_id,
            page_version=page.version,
            title=title,
            description=description,
            canonical=canonical,
            robots=robots,
        )
        validation = validate_meta(title=title, description=description, canonical=canonical, robots=robots)
        self.obs.emit("seo.meta.validated", metadata={"page_id": page_id, "passed": validation.passed})
        return {"snapshot_id": snap.snapshot_id, "issues": list(snap.issues), "validation": {"passed": validation.passed, "issues": list(validation.issues)}}

    def meta_generate(
        self,
        *,
        tenant_id: str,
        page_id: str,
        target_keyword: str,
        brand: str,
        product_facts: dict | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_META_GENERATE)
        page = self.store.get_page(page_id, tenant_id=tenant)
        if page is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        rec = generate_meta_recommendation(
            tenant_id=tenant,
            page_id=page_id,
            page_version=page.version,
            target_keyword=target_keyword,
            brand=brand,
            product_facts=product_facts,
        )
        self.store.save_recommendation(rec)
        self.obs.emit("seo.meta.generated", metadata={"recommendation_id": rec.recommendation_id})
        return {"recommendation_id": rec.recommendation_id, "status": rec.status, "validation": {"passed": rec.validation.passed}}

    def meta_apply(
        self,
        *,
        tenant_id: str,
        recommendation_id: str,
        idempotency_key: str,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_META_APPLY)
        rec = self.store.get_recommendation(recommendation_id, tenant_id=tenant)
        if rec is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        page = self.store.get_page(rec.page_id, tenant_id=tenant)
        if page is None:
            raise SeoMarketingError(SEO_PAGE_NOT_FOUND)
        if page.version != rec.page_version:
            raise SeoMarketingError(SEO_STALE_RECOMMENDATION)
        if not rec.validation.passed:
            raise SeoMarketingError(SEO_VALIDATION_FAILED)
        dedupe = f"{tenant}:{recommendation_id}:{idempotency_key}"
        if dedupe in self._applied_meta:
            return {"status": "idempotent", "recommendation_id": recommendation_id}
        external_ref = f"seo-meta:{recommendation_id}"
        if self.product_platform is not None and page.product_id:
            try:
                self.product_platform.cms_update_product(
                    tenant_id=tenant,
                    product_id=page.product_id,
                    version_id=str(page.version),
                    idempotency_key=idempotency_key,
                    capabilities=("catalog.write",),
                )
            except Exception:
                pass
        rec.status = META_STATUS_APPLIED
        self.store.save_recommendation(rec)
        self._applied_meta.add(dedupe)
        self.obs.emit("seo.meta.applied", metadata={"recommendation_id": recommendation_id, "external_ref": external_ref})
        return {"status": META_STATUS_APPLIED, "external_ref": external_ref, "recommendation_id": recommendation_id}

    def technical_audit(
        self,
        *,
        tenant_id: str,
        site_id: str,
        snapshot_pages: list[dict],
        links: list[dict] | None = None,
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_TECHNICAL_READ)
        assert_sync_seo_allowed(url_count=len(snapshot_pages), bulk=bulk)
        if self.acquisition is not None and not snapshot_pages:
            raise SeoMarketingError(SEO_VALIDATION_FAILED, "use acquisition snapshot; no direct HTTP")
        self.obs.emit("seo.technical.started", metadata={"site_id": site_id, "urls": len(snapshot_pages)})
        audit = analyze_technical_snapshot(
            tenant_id=tenant,
            site_id=site_id,
            snapshot_id=str(uuid.uuid4()),
            pages=snapshot_pages,
            links=links,
        )
        self.store.save_technical_audit(audit)
        self.obs.emit("seo.technical.completed", metadata={"audit_id": audit.audit_id, "issues": len(audit.issues)})
        return {"audit_id": audit.audit_id, "issue_count": len(audit.issues)}

    def performance_audit(
        self,
        *,
        tenant_id: str,
        site_id: str,
        page_ids: list[str],
        measurement_type: str = "LAB",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_PERFORMANCE_READ)
        assert_sync_seo_allowed(url_count=len(page_ids), bulk=bulk)
        self.obs.emit("seo.performance.started", metadata={"site_id": site_id})
        observations = []
        for pid in page_ids:
            page = self.store.get_page(pid, tenant_id=tenant)
            if page is None:
                continue
            result = self.performance.measure_url(tenant_id=tenant, url=page.url, measurement_type=measurement_type)
            for metric in (METRIC_LCP, METRIC_INP, METRIC_CLS):
                val = result.get(metric)
                observations.append(
                    build_performance_observation(
                        tenant_id=tenant,
                        page_id=pid,
                        metric=metric,
                        value=float(val) if val is not None else None,
                        unit="s" if metric == METRIC_LCP else ("ms" if metric == METRIC_INP else "score"),
                        measurement_type=measurement_type,
                        source=self.performance.provider_id,
                    )
                )
        audit = audit_performance(tenant_id=tenant, site_id=site_id, observations=observations)
        self.obs.emit("seo.performance.completed", metadata={"audit_id": audit.audit_id})
        return {"audit_id": audit.audit_id, "violations": list(audit.budget_violations)}

    def search_console_ingest(
        self,
        *,
        tenant_id: str,
        site_id: str,
        date_start: str,
        date_end: str,
        page_token: str = "",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_SEARCH_CONSOLE_READ)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        assert_sync_seo_allowed(sc_rows=BULK_URL_BATCH_SIZE if bulk else 1, bulk=bulk)
        snap = self.search_console.ingest(
            tenant_id=tenant,
            site_id=site_id,
            bound_property=site.search_console_property,
            property_id=site.search_console_property,
            date_start=date_start,
            date_end=date_end,
            page_token=page_token,
        )
        self.store.save_sc_snapshot(snap)
        self.obs.emit("seo.search_console.ingested", metadata={"snapshot_id": snap.snapshot_id, "rows": len(snap.rows)})
        return {"snapshot_id": snap.snapshot_id, "row_count": len(snap.rows), "freshness": snap.freshness}

    def analytics_ingest(
        self,
        *,
        tenant_id: str,
        site_id: str,
        date_start: str,
        date_end: str,
        page_token: str = "",
        bulk: bool = False,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_ANALYTICS_READ)
        site = self.store.get_site(site_id, tenant_id=tenant)
        if site is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        assert_sync_seo_allowed(analytics_rows=BULK_URL_BATCH_SIZE if bulk else 1, bulk=bulk)
        snap = self.analytics.ingest(
            tenant_id=tenant,
            site_id=site_id,
            bound_property=site.analytics_property,
            property_id=site.analytics_property,
            date_start=date_start,
            date_end=date_end,
            page_token=page_token,
        )
        self.store.save_analytics_snapshot(snap)
        self.obs.emit("seo.analytics.ingested", metadata={"snapshot_id": snap.snapshot_id})
        return {"snapshot_id": snap.snapshot_id, "row_count": len(snap.rows)}

    def optimization_plan(
        self,
        *,
        tenant_id: str,
        site_id: str,
        baseline_snapshot_ids: tuple[str, ...],
        actions: list[dict],
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._require_cap(capabilities, CAP_SEO_OPTIMIZATION_PLAN)
        plan = create_optimization_plan(
            tenant_id=tenant,
            site_id=site_id,
            baseline_snapshot_ids=baseline_snapshot_ids,
            actions=actions,
        )
        self.store.save_plan(plan)
        self.obs.emit("seo.optimization.planned", metadata={"plan_id": plan.plan_id})
        return {"plan_id": plan.plan_id, "version": plan.version}

    def optimization_measure(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        action_id: str,
        baseline_metrics: dict,
        post_metrics: dict,
        window_start: str,
        window_end: str,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        plan = self.store.get_plan(plan_id, tenant_id=tenant)
        if plan is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        measurement = measure_action(
            tenant_id=tenant,
            plan=plan,
            action_id=action_id,
            baseline_metrics=baseline_metrics,
            post_metrics=post_metrics,
            window_start=window_start,
            window_end=window_end,
        )
        self.obs.emit("seo.optimization.measured", metadata={"measurement_id": measurement.measurement_id})
        return {"measurement_id": measurement.measurement_id, "outcome": measurement.outcome}

    def optimization_decide(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        action_id: str,
        measurement_id: str | None = None,
        measurement: dict | None = None,
        days_since_action: int = 0,
        revisions_in_window: int = 0,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        plan = self.store.get_plan(plan_id, tenant_id=tenant)
        if plan is None:
            raise SeoMarketingError(SEO_SITE_NOT_FOUND)
        from seo_marketing.platform_models import OptimizationMeasurement

        meas_obj = None
        if measurement:
            meas_obj = OptimizationMeasurement(
                measurement_id=measurement_id or str(uuid.uuid4()),
                tenant_id=tenant,
                plan_id=plan_id,
                action_id=action_id,
                outcome=str(measurement.get("outcome") or "NO_CLEAR_CHANGE"),
                metrics=dict(measurement.get("metrics") or {}),
                window_start=str(measurement.get("window_start") or ""),
                window_end=str(measurement.get("window_end") or ""),
            )
        decision = decide_optimization(
            tenant_id=tenant,
            plan=plan,
            action_id=action_id,
            measurement=meas_obj,
            days_since_action=days_since_action,
            revisions_in_window=revisions_in_window,
        )
        self.store.save_decision(decision)
        self.obs.emit("seo.optimization.decided", metadata={"decision_id": decision.decision_id})
        return {"decision_id": decision.decision_id, "decision": decision.decision}

    def start_keyword_job(
        self,
        *,
        tenant_id: str,
        site_id: str,
        seeds: list[dict],
        job_id: str | None = None,
        bulk: bool = False,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        jid = job_id or str(uuid.uuid4())
        job = self.store.get_job(jid, tenant_id=tenant) or SeoJob(
            job_id=jid,
            tenant_id=tenant,
            operation="keyword_research",
            checkpoint=0,
            total=len(seeds),
            status="running",
            counts={"processed": 0, "failed": 0},
            payload={"seeds": seeds, "site_id": site_id},
        )
        start = int(job.checkpoint)
        end = min(start + BULK_KEYWORD_BATCH_SIZE, len(seeds))
        chunk = seeds[start:end]
        try:
            self.keyword_research(
                tenant_id=tenant,
                site_id=site_id,
                seeds=chunk,
                bulk=bulk,
                capabilities=(CAP_SEO_KEYWORD_ANALYZE,),
            )
            job.counts["processed"] = int(job.counts.get("processed", 0)) + len(chunk)
        except SeoMarketingError as exc:
            job.counts["failed"] = int(job.counts.get("failed", 0)) + len(chunk)
            job.status = "partial" if end < len(seeds) else "failed"
            job.payload["last_error"] = exc.code
        job.checkpoint = end
        job.status = "completed" if end >= len(seeds) else "partial"
        job.updated_at = _utc()
        self.store.save_job(job)
        return {"job_id": jid, "checkpoint": end, "status": job.status, "counts": dict(job.counts)}

    def start_technical_job(
        self,
        *,
        tenant_id: str,
        site_id: str,
        pages: list[dict],
        job_id: str | None = None,
        bulk: bool = True,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        jid = job_id or str(uuid.uuid4())
        job = self.store.get_job(jid, tenant_id=tenant) or SeoJob(
            job_id=jid,
            tenant_id=tenant,
            operation="technical_audit",
            checkpoint=0,
            total=len(pages),
            status="running",
            counts={"processed": 0, "failed": 0},
            payload={"site_id": site_id},
        )
        start = int(job.checkpoint)
        end = min(start + BULK_URL_BATCH_SIZE, len(pages))
        chunk = pages[start:end]
        result = self.technical_audit(
            tenant_id=tenant,
            site_id=site_id,
            snapshot_pages=chunk,
            bulk=bulk,
            capabilities=(CAP_SEO_TECHNICAL_READ,),
        )
        job.counts["processed"] = int(job.counts.get("processed", 0)) + len(chunk)
        job.checkpoint = end
        job.status = "completed" if end >= len(pages) else "partial"
        job.payload["last_audit_id"] = result.get("audit_id")
        job.updated_at = _utc()
        self.store.save_job(job)
        return {"job_id": jid, "checkpoint": end, "status": job.status, "counts": dict(job.counts)}

    def start_bulk_meta_apply(
        self,
        *,
        tenant_id: str,
        recommendation_ids: list[str],
        job_id: str | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        jid = job_id or str(uuid.uuid4())
        job = self.store.get_job(jid, tenant_id=tenant) or SeoJob(
            job_id=jid,
            tenant_id=tenant,
            operation="bulk_meta_apply",
            checkpoint=0,
            total=len(recommendation_ids),
            status="running",
            counts={"applied": 0, "stale": 0, "failed": 0, "skipped": 0},
            payload={},
        )
        start = int(job.checkpoint)
        end = min(start + BULK_META_APPLY_BATCH_SIZE, len(recommendation_ids))
        for rid in recommendation_ids[start:end]:
            try:
                self.meta_apply(
                    tenant_id=tenant,
                    recommendation_id=rid,
                    idempotency_key=f"bulk-{jid}-{rid}",
                    capabilities=capabilities or (CAP_SEO_META_APPLY,),
                )
                job.counts["applied"] = int(job.counts.get("applied", 0)) + 1
            except SeoMarketingError as exc:
                if exc.code == SEO_STALE_RECOMMENDATION:
                    job.counts["stale"] = int(job.counts.get("stale", 0)) + 1
                else:
                    job.counts["failed"] = int(job.counts.get("failed", 0)) + 1
        job.checkpoint = end
        job.status = "completed" if end >= len(recommendation_ids) else "partial"
        job.updated_at = _utc()
        self.store.save_job(job)
        return {"job_id": jid, "checkpoint": end, "status": job.status, "counts": dict(job.counts)}

    def reject_fake_metric(self, metric: dict) -> dict:
        trust = str(metric.get("trust_level") or "")
        source = str(metric.get("source") or "")
        if trust in {"MODEL_GENERATED", "MODEL_INFERRED"} and source not in {"trusted_provider", "search_console", "analytics"}:
            return {"accepted": False, "reason": "fake_metric_rejected"}
        return {"accepted": True}

    @staticmethod
    def compute_ctr(clicks: int, impressions: int):
        return compute_ctr(clicks, impressions)

    @staticmethod
    def compute_conversion_rate(conversions: int, sessions: int):
        return compute_conversion_rate(conversions, sessions)
