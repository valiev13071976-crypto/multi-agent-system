"""In-memory content store for tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, fields, is_dataclass
from typing import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from content_intel.platform_models import (
    BrandProfile,
    ContentAssetVersion,
    ContentExperiment,
    ContentHook,
    ContentIdea,
    ContentJob,
    ContentProject,
    ContentScript,
    ContentStrategy,
    MediaAssetRef,
    MediaBrief,
    OptimizationDecision,
    PerformanceObservation,
    PerformanceReport,
    PublicationPlan,
    ResearchReport,
    TrendSignal,
)
from content_intel.store import ContentStore
from security.tenant import normalize_tenant_id

DDL = """
CREATE TABLE IF NOT EXISTS content_records (
    tenant_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, record_type, record_id)
);
CREATE INDEX IF NOT EXISTS idx_content_tenant_type ON content_records(tenant_id, record_type);
"""


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(type(obj))


def _to_jsonable(obj):
    if is_dataclass(obj):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


class SqliteContentStore(ContentStore):
    def __init__(self, db_path: str | Path = ":memory:"):
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(DDL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _save(self, record_type: str, record_id: str, tenant_id: str, payload) -> None:
        tenant = normalize_tenant_id(tenant_id)
        body = _to_jsonable(payload)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO content_records
                (tenant_id, record_type, record_id, payload_json, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (tenant, record_type, record_id, json.dumps(body)),
            )
            self._conn.commit()

    def _get(self, record_type: str, record_id: str, tenant_id: str) -> dict | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT payload_json FROM content_records
                WHERE tenant_id = ? AND record_type = ? AND record_id = ?
                """,
                (tenant, record_type, record_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def _list(self, record_type: str, tenant_id: str) -> list[dict]:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM content_records WHERE tenant_id = ? AND record_type = ?",
                (tenant, record_type),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_project(self, project: ContentProject) -> ContentProject:
        self._save("project", project.project_id, project.tenant_id, project)
        return project

    def get_project(self, project_id: str, *, tenant_id: str) -> ContentProject | None:
        raw = self._get("project", project_id, tenant_id)
        return ContentProject(**raw) if raw else None

    def save_brand(self, brand: BrandProfile) -> BrandProfile:
        self._save("brand", brand.profile_id, brand.tenant_id, brand)
        return brand

    def get_brand(self, profile_id: str, *, tenant_id: str) -> BrandProfile | None:
        raw = self._get("brand", profile_id, tenant_id)
        return BrandProfile(**raw) if raw else None

    def save_research(self, report: ResearchReport) -> ResearchReport:
        self._save("research", report.report_id, report.tenant_id, report)
        return report

    def get_research(self, report_id: str, *, tenant_id: str) -> ResearchReport | None:
        raw = self._get("research", report_id, tenant_id)
        if not raw:
            return None
        from content_intel.platform_models import ResearchEvidence

        ev = tuple(ResearchEvidence(**e) for e in raw.pop("evidence", []))
        return ResearchReport(**raw, evidence=ev)

    def save_strategy(self, strategy: ContentStrategy) -> ContentStrategy:
        self._save("strategy", strategy.version_id, strategy.tenant_id, strategy)
        return strategy

    def get_strategy(self, version_id: str, *, tenant_id: str) -> ContentStrategy | None:
        raw = self._get("strategy", version_id, tenant_id)
        return ContentStrategy(**raw) if raw else None

    def save_idea(self, idea: ContentIdea) -> ContentIdea:
        self._save("idea", idea.version_id, idea.tenant_id, idea)
        return idea

    def save_hook(self, hook: ContentHook) -> ContentHook:
        self._save("hook", hook.hook_id, hook.tenant_id, hook)
        return hook

    def save_script(self, script: ContentScript) -> ContentScript:
        self._save("script", script.version_id, script.tenant_id, script)
        return script

    def save_asset(self, asset: ContentAssetVersion) -> ContentAssetVersion:
        self._save("asset", asset.version_id, asset.tenant_id, asset)
        return asset

    def get_asset(self, version_id: str, *, tenant_id: str) -> ContentAssetVersion | None:
        raw = self._get("asset", version_id, tenant_id)
        return ContentAssetVersion(**raw) if raw else None

    def list_assets(self, *, tenant_id: str, project_id: str) -> tuple[ContentAssetVersion, ...]:
        rows = self._list("asset", tenant_id)
        out = [ContentAssetVersion(**r) for r in rows if r.get("project_id") == project_id]
        return tuple(out)

    def save_media_brief(self, brief: MediaBrief) -> MediaBrief:
        self._save("media_brief", brief.brief_id, brief.tenant_id, brief)
        return brief

    def save_media_ref(self, ref: MediaAssetRef) -> MediaAssetRef:
        self._save("media_ref", ref.ref_id, ref.tenant_id, ref)
        return ref

    def save_plan(self, plan: PublicationPlan) -> PublicationPlan:
        self._save("plan", plan.version_id, plan.tenant_id, plan)
        return plan

    def save_observation(self, obs: PerformanceObservation) -> PerformanceObservation:
        self._save("observation", obs.observation_id, obs.tenant_id, obs)
        return obs

    def list_observations(
        self, *, tenant_id: str, asset_version_id: str | None = None
    ) -> tuple[PerformanceObservation, ...]:
        rows = self._list("observation", tenant_id)
        out = []
        for r in rows:
            if asset_version_id and r.get("asset_version_id") != asset_version_id:
                continue
            val = r.get("metric_value")
            if val is not None and not isinstance(val, Decimal):
                r["metric_value"] = Decimal(str(val))
            out.append(PerformanceObservation(**r))
        return tuple(out)

    def save_report(self, report: PerformanceReport) -> PerformanceReport:
        self._save("perf_report", report.report_id, report.tenant_id, report)
        return report

    def save_experiment(self, exp: ContentExperiment) -> ContentExperiment:
        self._save("experiment", exp.experiment_id, exp.tenant_id, exp)
        return exp

    def save_optimization(self, decision: OptimizationDecision) -> OptimizationDecision:
        self._save("optimization", decision.decision_id, decision.tenant_id, decision)
        if decision.idempotency_key:
            self._save(
                "optimization_key",
                decision.idempotency_key,
                decision.tenant_id,
                {"decision_id": decision.decision_id},
            )
        return decision

    def get_optimization_by_key(self, *, tenant_id: str, idempotency_key: str) -> OptimizationDecision | None:
        raw = self._get("optimization_key", idempotency_key, tenant_id)
        if not raw:
            return None
        return self.get_optimization(raw["decision_id"], tenant_id=tenant_id)

    def get_optimization(self, decision_id: str, *, tenant_id: str) -> OptimizationDecision | None:
        raw = self._get("optimization", decision_id, tenant_id)
        if not raw:
            return None
        win = raw.get("observation_window")
        if win and isinstance(win, list):
            raw["observation_window"] = tuple(datetime.fromisoformat(w) for w in win)
        return OptimizationDecision(**raw)

    def save_job(self, job: ContentJob) -> ContentJob:
        self._save("job", job.job_id, job.tenant_id, job)
        return job

    def get_job(self, job_id: str, *, tenant_id: str) -> ContentJob | None:
        raw = self._get("job", job_id, tenant_id)
        return ContentJob(**raw) if raw else None

    def save_trend(self, signal: TrendSignal) -> TrendSignal:
        self._save("trend", signal.trend_id, signal.tenant_id, signal)
        return signal
