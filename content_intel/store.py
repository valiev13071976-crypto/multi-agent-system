"""Content store contract — tenant-partitioned persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod

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


class ContentStore(ABC):
    @abstractmethod
    def save_project(self, project: ContentProject) -> ContentProject:
        raise NotImplementedError

    @abstractmethod
    def get_project(self, project_id: str, *, tenant_id: str) -> ContentProject | None:
        raise NotImplementedError

    @abstractmethod
    def save_brand(self, brand: BrandProfile) -> BrandProfile:
        raise NotImplementedError

    @abstractmethod
    def get_brand(self, profile_id: str, *, tenant_id: str) -> BrandProfile | None:
        raise NotImplementedError

    @abstractmethod
    def save_research(self, report: ResearchReport) -> ResearchReport:
        raise NotImplementedError

    @abstractmethod
    def get_research(self, report_id: str, *, tenant_id: str) -> ResearchReport | None:
        raise NotImplementedError

    @abstractmethod
    def save_strategy(self, strategy: ContentStrategy) -> ContentStrategy:
        raise NotImplementedError

    @abstractmethod
    def get_strategy(self, version_id: str, *, tenant_id: str) -> ContentStrategy | None:
        raise NotImplementedError

    @abstractmethod
    def save_idea(self, idea: ContentIdea) -> ContentIdea:
        raise NotImplementedError

    @abstractmethod
    def save_hook(self, hook: ContentHook) -> ContentHook:
        raise NotImplementedError

    @abstractmethod
    def save_script(self, script: ContentScript) -> ContentScript:
        raise NotImplementedError

    @abstractmethod
    def save_asset(self, asset: ContentAssetVersion) -> ContentAssetVersion:
        raise NotImplementedError

    @abstractmethod
    def get_asset(self, version_id: str, *, tenant_id: str) -> ContentAssetVersion | None:
        raise NotImplementedError

    @abstractmethod
    def list_assets(self, *, tenant_id: str, project_id: str) -> tuple[ContentAssetVersion, ...]:
        raise NotImplementedError

    @abstractmethod
    def save_media_brief(self, brief: MediaBrief) -> MediaBrief:
        raise NotImplementedError

    @abstractmethod
    def save_media_ref(self, ref: MediaAssetRef) -> MediaAssetRef:
        raise NotImplementedError

    @abstractmethod
    def save_plan(self, plan: PublicationPlan) -> PublicationPlan:
        raise NotImplementedError

    @abstractmethod
    def save_observation(self, obs: PerformanceObservation) -> PerformanceObservation:
        raise NotImplementedError

    @abstractmethod
    def list_observations(
        self, *, tenant_id: str, asset_version_id: str | None = None
    ) -> tuple[PerformanceObservation, ...]:
        raise NotImplementedError

    @abstractmethod
    def save_report(self, report: PerformanceReport) -> PerformanceReport:
        raise NotImplementedError

    @abstractmethod
    def save_experiment(self, exp: ContentExperiment) -> ContentExperiment:
        raise NotImplementedError

    @abstractmethod
    def save_optimization(self, decision: OptimizationDecision) -> OptimizationDecision:
        raise NotImplementedError

    @abstractmethod
    def get_optimization_by_key(self, *, tenant_id: str, idempotency_key: str) -> OptimizationDecision | None:
        raise NotImplementedError

    @abstractmethod
    def save_job(self, job: ContentJob) -> ContentJob:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str, *, tenant_id: str) -> ContentJob | None:
        raise NotImplementedError

    @abstractmethod
    def save_trend(self, signal: TrendSignal) -> TrendSignal:
        raise NotImplementedError
