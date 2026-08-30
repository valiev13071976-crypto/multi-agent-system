"""SQLite SEO store — tenant partitioned."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict

from seo_marketing.platform_models import (
    AnalyticsSnapshot,
    Keyword,
    MetaRecommendation,
    MetaValidationResult,
    OptimizationDecision,
    OptimizationPlan,
    SearchConsoleSnapshot,
    SeoJob,
    SeoPage,
    SeoProvenance,
    SeoSite,
    TechnicalSeoAudit,
    TechnicalSeoIssue,
)
from security.tenant import require_tenant_id


def _j(value) -> str:
    return json.dumps(value, default=str)


class SqliteSeoStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    def _conn(self):
        return self._connection

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seo_sites(
                    tenant_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, site_id)
                );
                CREATE TABLE IF NOT EXISTS seo_pages(
                    tenant_id TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, page_id)
                );
                CREATE TABLE IF NOT EXISTS seo_keywords(
                    tenant_id TEXT NOT NULL,
                    keyword_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, keyword_id)
                );
                CREATE TABLE IF NOT EXISTS seo_recommendations(
                    tenant_id TEXT NOT NULL,
                    recommendation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, recommendation_id)
                );
                CREATE TABLE IF NOT EXISTS seo_jobs(
                    tenant_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, job_id)
                );
                CREATE TABLE IF NOT EXISTS seo_plans(
                    tenant_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, plan_id)
                );
                CREATE TABLE IF NOT EXISTS seo_decisions(
                    tenant_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, decision_id)
                );
                CREATE TABLE IF NOT EXISTS seo_sc_snapshots(
                    tenant_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, snapshot_id)
                );
                CREATE TABLE IF NOT EXISTS seo_analytics_snapshots(
                    tenant_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, snapshot_id)
                );
                CREATE TABLE IF NOT EXISTS seo_technical_audits(
                    tenant_id TEXT NOT NULL,
                    audit_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, audit_id)
                );
                """
            )
            conn.commit()

    def save_site(self, site: SeoSite) -> None:
        tenant = require_tenant_id(site.tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_sites(tenant_id, site_id, payload_json) VALUES (?,?,?)",
                (tenant, site.site_id, _j(asdict(site))),
            )
            conn.commit()

    def get_site(self, site_id: str, *, tenant_id: str) -> SeoSite | None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT payload_json FROM seo_sites WHERE tenant_id=? AND site_id=?",
                (tenant, site_id),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload_json"])
        return SeoSite(**data)

    def save_page(self, page: SeoPage) -> None:
        tenant = require_tenant_id(page.tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_pages(tenant_id, page_id, site_id, payload_json) VALUES (?,?,?,?)",
                (tenant, page.page_id, page.site_id, _j(asdict(page))),
            )
            conn.commit()

    def get_page(self, page_id: str, *, tenant_id: str) -> SeoPage | None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT payload_json FROM seo_pages WHERE tenant_id=? AND page_id=?",
                (tenant, page_id),
            ).fetchone()
        if row is None:
            return None
        return SeoPage(**json.loads(row["payload_json"]))

    def list_pages(self, *, tenant_id: str, site_id: str) -> list[SeoPage]:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT payload_json FROM seo_pages WHERE tenant_id=? AND site_id=?",
                (tenant, site_id),
            ).fetchall()
        return [SeoPage(**json.loads(r["payload_json"])) for r in rows]

    def save_keyword(self, keyword: Keyword) -> None:
        tenant = require_tenant_id(keyword.tenant_id)
        payload = asdict(keyword)
        payload["provenance"] = asdict(keyword.provenance)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_keywords(tenant_id, keyword_id, site_id, payload_json) VALUES (?,?,?,?)",
                (tenant, keyword.keyword_id, keyword.site_id, _j(payload)),
            )
            conn.commit()

    def list_keywords(self, *, tenant_id: str, site_id: str) -> list[Keyword]:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT payload_json FROM seo_keywords WHERE tenant_id=? AND site_id=?",
                (tenant, site_id),
            ).fetchall()
        out = []
        for r in rows:
            data = json.loads(r["payload_json"])
            prov = SeoProvenance(**data.pop("provenance"))
            out.append(Keyword(provenance=prov, **data))
        return out

    def save_recommendation(self, rec: MetaRecommendation) -> None:
        tenant = require_tenant_id(rec.tenant_id)
        payload = asdict(rec)
        payload["validation"] = asdict(rec.validation)
        payload["provenance"] = asdict(rec.provenance)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_recommendations(tenant_id, recommendation_id, payload_json) VALUES (?,?,?)",
                (tenant, rec.recommendation_id, _j(payload)),
            )
            conn.commit()

    def get_recommendation(self, recommendation_id: str, *, tenant_id: str) -> MetaRecommendation | None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT payload_json FROM seo_recommendations WHERE tenant_id=? AND recommendation_id=?",
                (tenant, recommendation_id),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload_json"])
        validation = MetaValidationResult(**data.pop("validation"))
        provenance = SeoProvenance(**data.pop("provenance"))
        return MetaRecommendation(validation=validation, provenance=provenance, **data)

    def save_job(self, job: SeoJob) -> None:
        tenant = require_tenant_id(job.tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_jobs(tenant_id, job_id, payload_json) VALUES (?,?,?)",
                (tenant, job.job_id, _j(asdict(job))),
            )
            conn.commit()

    def get_job(self, job_id: str, *, tenant_id: str) -> SeoJob | None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT payload_json FROM seo_jobs WHERE tenant_id=? AND job_id=?",
                (tenant, job_id),
            ).fetchone()
        if row is None:
            return None
        return SeoJob(**json.loads(row["payload_json"]))

    def save_plan(self, plan: OptimizationPlan) -> None:
        tenant = require_tenant_id(plan.tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_plans(tenant_id, plan_id, payload_json) VALUES (?,?,?)",
                (tenant, plan.plan_id, _j(asdict(plan))),
            )
            conn.commit()

    def get_plan(self, plan_id: str, *, tenant_id: str) -> OptimizationPlan | None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT payload_json FROM seo_plans WHERE tenant_id=? AND plan_id=?",
                (tenant, plan_id),
            ).fetchone()
        if row is None:
            return None
        return OptimizationPlan(**json.loads(row["payload_json"]))

    def save_decision(self, decision: OptimizationDecision) -> None:
        tenant = require_tenant_id(decision.tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_decisions(tenant_id, decision_id, payload_json) VALUES (?,?,?)",
                (tenant, decision.decision_id, _j(asdict(decision))),
            )
            conn.commit()

    def save_sc_snapshot(self, snap: SearchConsoleSnapshot) -> None:
        tenant = require_tenant_id(snap.tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_sc_snapshots(tenant_id, snapshot_id, payload_json) VALUES (?,?,?)",
                (tenant, snap.snapshot_id, _j(asdict(snap))),
            )
            conn.commit()

    def save_analytics_snapshot(self, snap: AnalyticsSnapshot) -> None:
        tenant = require_tenant_id(snap.tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_analytics_snapshots(tenant_id, snapshot_id, payload_json) VALUES (?,?,?)",
                (tenant, snap.snapshot_id, _j(asdict(snap))),
            )
            conn.commit()

    def save_technical_audit(self, audit: TechnicalSeoAudit) -> None:
        tenant = require_tenant_id(audit.tenant_id)
        payload = asdict(audit)
        payload["issues"] = [asdict(i) for i in audit.issues]
        payload["provenance"] = asdict(audit.provenance)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO seo_technical_audits(tenant_id, audit_id, payload_json) VALUES (?,?,?)",
                (tenant, audit.audit_id, _j(payload)),
            )
            conn.commit()
