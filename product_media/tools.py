"""Tool Platform adapter for Product Media Intelligence (Block 10)."""

from __future__ import annotations

from product_media.errors import MediaBatchRequired, MediaError
from tools.errors import ToolNotFoundError

# Relative path prefix for the safe, authorized artifact URL served by the existing
# business_assistant_api auth boundary (session cookie or X-API-Key — see
# business_assistant_api.router.get_media). Kept as a local literal (not an import of
# business_assistant_api) to avoid a layering dependency from product_media -> the API layer.
_MEDIA_VIEW_URL_PREFIX = "/api/v1/business-assistant/media"

# Conversational chat tool ids (Panda action-continuation). Distinct from the governed
# "media.generate"/"media.edit" business-publishing tools which remain write-governed.
_CHAT_IMAGE_GENERATE_TOOL_ID = "image.generate"
_CHAT_IMAGE_EDIT_TOOL_ID = "image.edit"


def media_view_url(version_id: str) -> str:
    """Safe, tenant-authorized artifact URL for a persisted media version.

    The URL itself carries no secret/tenant data; the serving endpoint re-derives the
    tenant from the authenticated request and denies cross-tenant access.
    """
    vid = str(version_id or "").strip()
    return f"{_MEDIA_VIEW_URL_PREFIX}/{vid}" if vid else ""


class ProductMediaToolAdapter:
    adapter_id = "product_media"

    def __init__(self, service=None):
        self.service = service

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("media.") or tool_id in {"image.generate", "image.edit"}

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self.service is not None else ADAPTER_UNAVAILABLE

    def _tenant(self, request) -> str:
        return str(request.tenant_id or "legacy-default")

    async def execute_read(self, request, context) -> dict:
        if self.service is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        tenant = self._tenant(request)
        op = request.operation
        if request.tool_id == _CHAT_IMAGE_GENERATE_TOOL_ID:
            return self._chat_generate(tenant, args)
        if request.tool_id == _CHAT_IMAGE_EDIT_TOOL_ID:
            return self._chat_edit(tenant, args)
        payload = {"tenant_id": tenant, **args}
        if op in {"get", "analyze", "find_similar", "find_duplicates"}:
            mapping = {
                "get": "media.get",
                "analyze": "media.analyze",
                "find_similar": "media.find_similar",
                "find_duplicates": "media.find_duplicates",
            }
            if op == "find_similar":
                return {"results": self.service.find_similar(tenant_id=tenant, version_id=str(args["version_id"]))}
            if op == "find_duplicates":
                return {"duplicates": self.service.find_duplicates(tenant_id=tenant, version_id=str(args["version_id"]))}
            return self.service.dispatch(mapping.get(op, f"media.{op}"), payload)
        raise ToolNotFoundError("operation_not_supported")

    def _chat_generate(self, tenant: str, args: dict) -> dict:
        try:
            result = self.service.generate_from_brief(
                tenant_id=tenant,
                scene_description=str(args.get("scene_description") or args.get("prompt") or ""),
                aspect_ratio=str(args.get("aspect_ratio") or "1:1"),
                variant_count=int(args.get("variant_count") or 1),
                bulk=bool(args.get("bulk", False)),
                media_brief_id=str(args.get("media_brief_id") or ""),
            )
        except MediaError as exc:
            return {"status": "error", "reason": exc.code}
        version_ids = [str(v) for v in (result.get("version_ids") or [])]
        assets = []
        mime_type = "image/png"
        for vid in version_ids:
            version = self.service.get(tenant_id=tenant, version_id=vid)
            mime_type = str(getattr(version, "mime_type", "") or mime_type)
            assets.append(
                {
                    "version_id": vid,
                    "artifact_type": "image",
                    "mime_type": mime_type,
                    "view_url": media_view_url(vid),
                }
            )
        return {
            "version_ids": version_ids,
            "assets": assets,
            "mime_type": mime_type,
            "view_url": assets[0]["view_url"] if assets else "",
            "status": result.get("status"),
            "failed": result.get("failed", 0),
        }

    def _chat_edit(self, tenant: str, args: dict) -> dict:
        try:
            version = self.service.edit(
                tenant_id=tenant,
                source_version_id=str(args["source_version_id"]),
                instruction=str(args.get("instruction") or ""),
                mask_version_id=args.get("mask_version_id"),
            )
        except MediaError as exc:
            return {"status": "error", "reason": exc.code}
        url = media_view_url(version.version_id)
        return {
            "version_id": version.version_id,
            "version_ids": [version.version_id],
            "assets": [
                {
                    "version_id": version.version_id,
                    "artifact_type": "image",
                    "mime_type": version.mime_type,
                    "view_url": url,
                }
            ],
            "mime_type": version.mime_type,
            "view_url": url,
            "status": "completed",
        }

    async def execute_write(self, request, context) -> dict:
        if self.service is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        tenant = self._tenant(request)
        op = request.operation
        try:
            if op == "ingest":
                version = self.service.ingest(
                    tenant_id=tenant,
                    data=args["data"],
                    filename=str(args.get("filename") or ""),
                    declared_mime=str(args.get("mime_type") or ""),
                    payload_tenant=args.get("payload_tenant"),
                )
                return {"version_id": version.version_id, "media_id": version.media_id, "hash": version.content_hash}
            if op == "generate" or request.tool_id == "image.generate":
                return self.service.generate_from_brief(
                    tenant_id=tenant,
                    scene_description=str(args.get("scene_description") or args.get("prompt") or ""),
                    aspect_ratio=str(args.get("aspect_ratio") or "1:1"),
                    variant_count=int(args.get("variant_count") or 1),
                    bulk=bool(args.get("bulk", False)),
                    media_brief_id=str(args.get("media_brief_id") or ""),
                )
            if op == "transform":
                transform = str(args.get("transform") or "resize")
                if transform == "resize":
                    version = self.service.transform_resize(
                        tenant_id=tenant,
                        version_id=str(args["version_id"]),
                        width=int(args["width"]),
                        height=int(args["height"]),
                        fit=str(args.get("fit") or "contain"),
                    )
                elif transform == "crop":
                    version = self.service.transform_crop(
                        tenant_id=tenant,
                        version_id=str(args["version_id"]),
                        left=int(args["left"]),
                        top=int(args["top"]),
                        width=int(args["width"]),
                        height=int(args["height"]),
                    )
                elif transform == "thumbnail":
                    version = self.service.thumbnail(tenant_id=tenant, version_id=str(args["version_id"]))
                elif transform == "remove_background":
                    version = self.service.remove_background(tenant_id=tenant, version_id=str(args["version_id"]))
                else:
                    version = self.service.strip_metadata(tenant_id=tenant, version_id=str(args["version_id"]))
                return {"version_id": version.version_id}
            if op == "edit" or request.tool_id == "image.edit":
                version = self.service.edit(
                    tenant_id=tenant,
                    source_version_id=str(args["source_version_id"]),
                    instruction=str(args.get("instruction") or ""),
                    mask_version_id=args.get("mask_version_id"),
                )
                return {"version_id": version.version_id}
            if op == "delete":
                result = self.service.delete(tenant_id=tenant, version_id=str(args["version_id"]))
                return {"status": result.status}
            if op == "link_product":
                link = self.service.link_product(
                    tenant_id=tenant,
                    version_id=str(args["version_id"]),
                    product_id=str(args["product_id"]),
                    sku=str(args.get("sku") or ""),
                    link_state=str(args.get("link_state") or "CANDIDATE"),
                    source=str(args.get("source") or "explicit"),
                )
                return {"link_id": link.link_id, "link_state": link.link_state}
            if op == "validate_set":
                media_set = self.service.validate_set(
                    tenant_id=tenant,
                    product_id=str(args["product_id"]),
                    items=list(args.get("items") or []),
                    profile=str(args.get("profile") or "marketplace"),
                )
                return {"set_id": media_set.set_id, "errors": list(media_set.validation_errors)}
        except MediaBatchRequired as exc:
            return {"status": "MEDIA_BATCH_REQUIRED", "reason": exc.code}
        except MediaError as exc:
            return {"status": "error", "reason": exc.code}
        raise ToolNotFoundError("operation_not_supported")

    def dispatch(self, operation: str, payload: dict) -> dict:
        """Sync helper for tests."""
        if self.service is None:
            raise ToolNotFoundError("tool_unavailable")
        tenant_id = payload["tenant_id"]
        if operation == "media.get":
            version = self.service.get(tenant_id=tenant_id, version_id=payload["version_id"])
            return {"version": None if version is None else version.version_id}
        if operation == "media.analyze":
            return self.service.analyze(
                tenant_id=tenant_id, version_id=payload["version_id"], profile=payload.get("profile", "website")
            )
        if operation == "media.generate":
            return self.service.generate_from_brief(
                tenant_id=tenant_id,
                scene_description=payload.get("scene_description", ""),
                aspect_ratio=payload.get("aspect_ratio", "1:1"),
                variant_count=int(payload.get("variant_count", 1)),
                bulk=bool(payload.get("bulk", False)),
                media_brief_id=payload.get("media_brief_id", ""),
            )
        raise KeyError(operation)
