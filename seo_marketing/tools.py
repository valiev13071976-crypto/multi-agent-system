"""Tool platform adapter for SEO & Digital Marketing (Block 12)."""

from __future__ import annotations

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
from seo_marketing.errors import SeoBatchRequired, SeoMarketingError
from seo_marketing.planner import assert_sync_seo_allowed
from seo_marketing.service import SeoMarketingService
from tools.errors import ToolArgumentInvalidError, ToolAuthFailedError, ToolNotFoundError, ToolPermanentFailureError, ToolUnavailableError


class SeoMarketingToolAdapter:
    adapter_id = "seo_marketing"

    def __init__(self, service: SeoMarketingService | None = None, *, enabled: bool = False):
        self._service = service
        self._enabled = enabled and service is not None

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("seo.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._enabled else ADAPTER_UNAVAILABLE

    def _caps(self, request) -> tuple[str, ...]:
        return tuple(getattr(request, "requested_capabilities", None) or ())

    def _tenant(self, request) -> str:
        return str(request.tenant_id or "legacy-default")

    async def execute_read(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        tenant = self._tenant(request)
        args = dict(request.arguments or {})
        op = request.operation
        svc = self._service
        caps = self._caps(request)
        try:
            if op in {"keyword_opportunities", "opportunities"}:
                return svc.keyword_opportunities(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    metric_rows=list(args.get("metric_rows") or []),
                    capabilities=caps,
                )
            if op in {"meta_inspect", "inspect"}:
                return svc.meta_inspect(
                    tenant_id=tenant,
                    page_id=str(args["page_id"]),
                    title=str(args.get("title") or ""),
                    description=str(args.get("description") or ""),
                    canonical=str(args.get("canonical") or ""),
                    robots=str(args.get("robots") or ""),
                    capabilities=caps,
                )
            if op in {"search_console_query", "search_console_ingest"}:
                return svc.search_console_ingest(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    date_start=str(args["date_start"]),
                    date_end=str(args["date_end"]),
                    page_token=str(args.get("page_token") or ""),
                    bulk=bool(args.get("bulk")),
                    capabilities=caps or (CAP_SEO_SEARCH_CONSOLE_READ,),
                )
            if op in {"analytics_query", "analytics_ingest"}:
                return svc.analytics_ingest(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    date_start=str(args["date_start"]),
                    date_end=str(args["date_end"]),
                    page_token=str(args.get("page_token") or ""),
                    bulk=bool(args.get("bulk")),
                    capabilities=caps or (CAP_SEO_ANALYTICS_READ,),
                )
            if op == "technical_page":
                return svc.technical_audit(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    snapshot_pages=[dict(args.get("page") or args)],
                    capabilities=caps or (CAP_SEO_TECHNICAL_READ,),
                )
            if op == "performance_page":
                return svc.performance_audit(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    page_ids=[str(args["page_id"])],
                    capabilities=caps or (CAP_SEO_PERFORMANCE_READ,),
                )
        except SeoBatchRequired as exc:
            raise ToolArgumentInvalidError(str(exc.code)) from exc
        except SeoMarketingError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolNotFoundError("operation_not_supported")

    async def execute_write(self, request, context) -> dict:
        if not self._enabled:
            raise ToolUnavailableError()
        tenant = self._tenant(request)
        args = dict(request.arguments or {})
        op = request.operation
        svc = self._service
        caps = self._caps(request)
        try:
            if op in {"keyword_research", "research"}:
                seeds = list(args.get("seeds") or [])
                try:
                    assert_sync_seo_allowed(keyword_count=len(seeds), bulk=bool(args.get("bulk")))
                except SeoBatchRequired as exc:
                    raise ToolArgumentInvalidError(str(exc.code)) from exc
                return svc.keyword_research(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    seeds=seeds,
                    source=str(args.get("source") or "seed"),
                    bulk=bool(args.get("bulk")),
                    capabilities=caps or (CAP_SEO_KEYWORD_ANALYZE,),
                )
            if op in {"keyword_cluster", "cluster"}:
                return svc.keyword_cluster(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    capabilities=caps or (CAP_SEO_KEYWORD_ANALYZE,),
                )
            if op in {"meta_generate", "generate"}:
                return svc.meta_generate(
                    tenant_id=tenant,
                    page_id=str(args["page_id"]),
                    target_keyword=str(args["target_keyword"]),
                    brand=str(args.get("brand") or "Brand"),
                    product_facts=dict(args.get("product_facts") or {}),
                    capabilities=caps or (CAP_SEO_META_GENERATE,),
                )
            if op in {"meta_apply", "apply", "metadata_write"}:
                if CAP_SEO_META_APPLY not in caps and caps:
                    raise ToolAuthFailedError("tool_permission_denied")
                return svc.meta_apply(
                    tenant_id=tenant,
                    recommendation_id=str(args["recommendation_id"]),
                    idempotency_key=str(args.get("idempotency_key") or request.request_id),
                    capabilities=caps or (CAP_SEO_META_APPLY,),
                )
            if op in {"technical_audit", "audit"}:
                pages = list(args.get("pages") or [])
                try:
                    assert_sync_seo_allowed(url_count=len(pages), bulk=bool(args.get("bulk")))
                except SeoBatchRequired as exc:
                    raise ToolArgumentInvalidError(str(exc.code)) from exc
                return svc.technical_audit(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    snapshot_pages=pages,
                    links=list(args.get("links") or []),
                    bulk=bool(args.get("bulk")),
                    capabilities=caps or (CAP_SEO_TECHNICAL_READ,),
                )
            if op == "performance_audit":
                return svc.performance_audit(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    page_ids=list(args.get("page_ids") or []),
                    bulk=bool(args.get("bulk")),
                    capabilities=caps or (CAP_SEO_PERFORMANCE_READ,),
                )
            if op == "optimization_plan":
                return svc.optimization_plan(
                    tenant_id=tenant,
                    site_id=str(args["site_id"]),
                    baseline_snapshot_ids=tuple(args.get("baseline_snapshot_ids") or ()),
                    actions=list(args.get("actions") or []),
                    capabilities=caps or (CAP_SEO_OPTIMIZATION_PLAN,),
                )
            if op == "optimization_measure":
                return svc.optimization_measure(
                    tenant_id=tenant,
                    plan_id=str(args["plan_id"]),
                    action_id=str(args["action_id"]),
                    baseline_metrics=dict(args.get("baseline_metrics") or {}),
                    post_metrics=dict(args.get("post_metrics") or {}),
                    window_start=str(args["window_start"]),
                    window_end=str(args["window_end"]),
                )
            if op == "optimization_decide":
                return svc.optimization_decide(
                    tenant_id=tenant,
                    plan_id=str(args["plan_id"]),
                    action_id=str(args["action_id"]),
                    measurement=dict(args.get("measurement") or {}),
                    days_since_action=int(args.get("days_since_action") or 0),
                    revisions_in_window=int(args.get("revisions_in_window") or 0),
                )
        except SeoBatchRequired as exc:
            raise ToolArgumentInvalidError(str(exc.code)) from exc
        except SeoMarketingError as exc:
            raise ToolPermanentFailureError(exc.code) from exc
        raise ToolNotFoundError("operation_not_supported")
