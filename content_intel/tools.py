"""Tool Platform adapter for Content Intelligence."""

from __future__ import annotations

from content_intel.errors import ContentBatchRequired, ContentIntelError, ContentInsufficientEvidence
from content_intel.planner import assert_sync_content_allowed
from tools.errors import ToolArgumentInvalidError, ToolNotFoundError


class ContentIntelToolAdapter:
    adapter_id = "content_intel"

    def __init__(self, service=None):
        self._svc = service

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("content.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._svc is not None else ADAPTER_UNAVAILABLE

    def _tenant(self, request) -> str:
        return str(request.tenant_id or "legacy-default")

    async def execute_read(self, request, context) -> dict:
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        tenant = self._tenant(request)
        op = request.operation
        if op == "get":
            vid = str(args.get("asset_version_id") or "")
            asset = self._svc.get_asset(vid, tenant_id=tenant)
            if asset is None:
                return {"found": False}
            return {"found": True, "version_id": asset.version_id, "status": asset.status}
        if op == "status":
            return {"enabled": True}
        if op == "analyze_performance":
            return self._svc.ingest_performance(
                tenant_id=tenant,
                project_id=str(args.get("project_id") or ""),
                rows=list(args.get("observations") or []),
            )
        raise ToolNotFoundError("operation_not_supported")

    async def execute_write(self, request, context) -> dict:
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        tenant = self._tenant(request)
        op = request.operation
        if op == "research":
            rows = list(args.get("evidence") or [])
            try:
                assert_sync_content_allowed(item_count=len(rows), bulk=bool(args.get("bulk")))
            except ContentBatchRequired as exc:
                raise ToolArgumentInvalidError(str(exc.code)) from exc
            report = self._svc.research(
                tenant_id=tenant,
                project_id=str(args.get("project_id") or ""),
                objective_id=str(args.get("objective_id") or ""),
                evidence_rows=rows,
                bulk=bool(args.get("bulk")),
            )
            return {"report_id": report.report_id, "grounding": report.grounding}
        if op == "create_strategy":
            strategy = self._svc.create_strategy(
                tenant_id=tenant,
                project_id=str(args.get("project_id") or ""),
                objective=str(args.get("objective") or ""),
                channel=str(args.get("channel") or "social"),
                audience_segments=tuple(args.get("audience_segments") or ("general",)),
                evidence_refs=tuple(args.get("evidence_refs") or ()),
            )
            return {"strategy_version_id": strategy.version_id}
        if op == "generate_copy":
            try:
                assert_sync_content_allowed(item_count=1)
            except ContentBatchRequired as exc:
                raise ToolArgumentInvalidError(str(exc.code)) from exc
            asset = self._svc.generate_copy(
                tenant_id=tenant,
                project_id=str(args.get("project_id") or ""),
                content_type=str(args.get("content_type") or "social_post"),
                channel=str(args.get("channel") or "social"),
                objective=str(args.get("objective") or ""),
                product_facts=dict(args.get("product_facts") or {}),
            )
            return {"asset_version_id": asset.version_id, "status": asset.status}
        if op == "generate_media":
            brief = self._svc.create_media_brief(
                tenant_id=tenant,
                asset_version_id=str(args.get("asset_version_id") or ""),
                media_type=str(args.get("media_type") or "image"),
                aspect_ratio=str(args.get("aspect_ratio") or "1:1"),
                scene_description=str(args.get("scene_description") or ""),
            )
            ref = self._svc.generate_media(tenant_id=tenant, brief=brief)
            return {"media_ref_id": ref.ref_id, "artifact_id": ref.artifact_id}
        if op == "create_publication_plan":
            plan = self._svc.create_publication_plan(
                tenant_id=tenant,
                project_id=str(args.get("project_id") or ""),
                items=list(args.get("items") or []),
            )
            return {"plan_version_id": plan.version_id}
        if op == "optimize":
            try:
                decision = self._svc.optimize(
                    tenant_id=tenant,
                    project_id=str(args.get("project_id") or ""),
                    strategy_version_id=str(args.get("strategy_version_id") or ""),
                    asset_version_ids=tuple(args.get("asset_version_ids") or ()),
                    observation_window=tuple(args.get("observation_window") or ()),
                    metrics=dict(args.get("metrics") or {}),
                )
            except ContentInsufficientEvidence as exc:
                return {"status": "INSUFFICIENT_EVIDENCE", "reason": exc.reason}
            return {
                "decision_id": decision.decision_id,
                "recommended_action": decision.recommended_action,
            }
        raise ToolNotFoundError("operation_not_supported")
