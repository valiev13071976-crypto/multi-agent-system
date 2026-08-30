"""Tool permission aliases and authorize_tool_request (default deny)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from autonomy.capabilities import (
    CAP_BROWSER_READ,
    CAP_BROWSER_WRITE,
    CAP_CALENDAR_READ,
    CAP_CALENDAR_WRITE,
    CAP_CRM_READ,
    CAP_CRM_WRITE,
    CAP_DB_READ,
    CAP_DB_WRITE,
    CAP_EMAIL_READ,
    CAP_EMAIL_SEND,
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CAP_FILESYSTEM_READ,
    CAP_FILESYSTEM_WRITE,
    CAP_IMAGE_EDIT,
    CAP_IMAGE_GENERATE,
    CAP_MCP_INVOKE,
    CAP_MESSAGE_SEND,
    CAP_SCRAPE,
    CAP_SEO_READ,
    CAP_SEO_WRITE,
    CAP_SITE_READ,
    CAP_SITE_WRITE,
    CAP_TELEGRAM_READ,
    CAP_TELEGRAM_SEND,
    CapabilityScope,
    matches_resource,
)
from tools.errors import ToolPermissionDeniedError
from tools.models import (
    APPROVAL_POLICY_REQUIRED,
    TOOL_TRUST_PRIVILEGED,
    ToolDescriptor,
    ToolRequest,
)

# Human / platform aliases → CAP_* tokens
CAPABILITY_ALIASES: Mapping[str, str] = {
    "files.read": CAP_FILESYSTEM_READ,
    "files.write": CAP_FILESYSTEM_WRITE,
    "filesystem.read": CAP_FILESYSTEM_READ,
    "filesystem.write": CAP_FILESYSTEM_WRITE,
    "web.search": CAP_EXTERNAL_READ,
    "external.read": CAP_EXTERNAL_READ,
    "external.write": CAP_EXTERNAL_WRITE,
    "browser.read": CAP_BROWSER_READ,
    "browser.write": CAP_BROWSER_WRITE,
    "email.read": CAP_EMAIL_READ,
    "email.send": CAP_EMAIL_SEND,
    "calendar.read": CAP_CALENDAR_READ,
    "calendar.write": CAP_CALENDAR_WRITE,
    "calendar.*": CAP_CALENDAR_READ,
    "crm.read": CAP_CRM_READ,
    "crm.write": CAP_CRM_WRITE,
    "crm.*": CAP_CRM_READ,
    "cms.read": CAP_SITE_READ,
    "cms.write": CAP_SITE_WRITE,
    "cms.*": CAP_SITE_READ,
    "db.read": CAP_DB_READ,
    "db.write": CAP_DB_WRITE,
    "telegram.read": CAP_TELEGRAM_READ,
    "telegram.send": CAP_TELEGRAM_SEND,
    "telegram.*": CAP_TELEGRAM_READ,
    "message.send": CAP_MESSAGE_SEND,
    "image.generate": CAP_IMAGE_GENERATE,
    "image.edit": CAP_IMAGE_EDIT,
    "scrape": CAP_SCRAPE,
    "scrape.fetch": CAP_SCRAPE,
    "seo.read": CAP_SEO_READ,
    "seo.write": CAP_SEO_WRITE,
    "mcp.invoke": CAP_MCP_INVOKE,
}


def normalize_capability(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    if raw in CAPABILITY_ALIASES:
        return CAPABILITY_ALIASES[raw]
    lowered = raw.lower()
    if lowered in CAPABILITY_ALIASES:
        return CAPABILITY_ALIASES[lowered]
    return raw


def expand_capabilities(names) -> frozenset[str]:
    out: set[str] = set()
    for name in names or ():
        cap = normalize_capability(str(name))
        if cap:
            out.add(cap)
    return frozenset(out)


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason_code: str
    required: tuple[str, ...] = ()
    provided: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "required": list(self.required),
            "provided": list(self.provided),
        }


def _provided_caps(capabilities) -> set[str]:
    if capabilities is None:
        return set()
    if hasattr(capabilities, "capabilities"):
        return set(expand_capabilities(capabilities.capabilities))
    if isinstance(capabilities, (set, list, tuple, frozenset)):
        return set(expand_capabilities(capabilities))
    return set()


def _scope_from_capabilities(capabilities) -> CapabilityScope | None:
    if capabilities is None:
        return None
    scope = getattr(capabilities, "scope", None)
    if isinstance(scope, CapabilityScope):
        return scope
    return None


def authorize_tool_request(
    *,
    request: ToolRequest,
    descriptor: ToolDescriptor,
    capabilities=None,
    actor_id: str | None = None,
    tenant_id: str | None = None,
    data_scope_ref: str | None = None,
    resource: str | None = None,
    raise_on_deny: bool = True,
) -> AuthorizationDecision:
    """Default-deny authorization. Trust alone does NOT grant execution."""

    required = tuple(descriptor.capabilities_required or ())
    provided = _provided_caps(capabilities)
    # Also accept aliases listed on the request
    provided |= set(expand_capabilities(request.requested_capabilities or ()))

    actor = str(actor_id if actor_id is not None else request.actor_id or "").strip()
    tenant = str(tenant_id if tenant_id is not None else request.tenant_id or "").strip()

    if descriptor.tenant_scope_policy == "required" and not tenant:
        decision = AuthorizationDecision(
            False, "tenant_required", required=required, provided=tuple(sorted(provided))
        )
        if raise_on_deny:
            raise ToolPermissionDeniedError("tenant_required")
        return decision

    if not required:
        # Still default-deny privileged tools without explicit grant path
        if descriptor.trust_level == TOOL_TRUST_PRIVILEGED and not provided:
            decision = AuthorizationDecision(
                False, "privileged_denied", required=required, provided=()
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("privileged_denied")
            return decision
        # No required caps → still need an authenticated actor context for writes
        if not descriptor.read_only and not actor and not provided:
            decision = AuthorizationDecision(
                False, "unauthorized", required=required, provided=()
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("unauthorized")
            return decision
    else:
        if not provided:
            decision = AuthorizationDecision(
                False, "unauthorized", required=required, provided=()
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("unauthorized")
            return decision
        if not set(required) <= provided:
            decision = AuthorizationDecision(
                False,
                "missing_tool_capability",
                required=required,
                provided=tuple(sorted(provided)),
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("missing_tool_capability")
            return decision

    # Data scope / resource pattern enforcement
    scope = _scope_from_capabilities(capabilities)
    scope_ref = str(
        data_scope_ref
        if data_scope_ref is not None
        else (request.data_scope_ref or "")
    ).strip()
    resource_value = str(
        resource
        if resource is not None
        else (dict(request.arguments or {}).get("resource") or scope_ref or "")
    ).strip()

    if scope is not None:
        if scope.tool_id and scope.tool_id != descriptor.tool_id:
            decision = AuthorizationDecision(
                False, "scope_tool_mismatch", required=required, provided=tuple(sorted(provided))
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("scope_tool_mismatch")
            return decision
        if scope.operation and scope.operation != request.operation:
            decision = AuthorizationDecision(
                False,
                "scope_operation_mismatch",
                required=required,
                provided=tuple(sorted(provided)),
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("scope_operation_mismatch")
            return decision
        if scope.resource_pattern and resource_value:
            if not scope.matches_resource(resource_value):
                decision = AuthorizationDecision(
                    False,
                    "scope_resource_mismatch",
                    required=required,
                    provided=tuple(sorted(provided)),
                )
                if raise_on_deny:
                    raise ToolPermissionDeniedError("scope_resource_mismatch")
                return decision
        elif scope.resource_pattern and not resource_value:
            decision = AuthorizationDecision(
                False,
                "scope_resource_mismatch",
                required=required,
                provided=tuple(sorted(provided)),
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("scope_resource_mismatch")
            return decision

    if scope_ref and descriptor.resource_prefix:
        if not matches_resource(f"{descriptor.resource_prefix}*", scope_ref) and not scope_ref.startswith(
            descriptor.resource_prefix
        ):
            decision = AuthorizationDecision(
                False,
                "data_scope_denied",
                required=required,
                provided=tuple(sorted(provided)),
            )
            if raise_on_deny:
                raise ToolPermissionDeniedError("data_scope_denied")
            return decision

    # Trust alone never grants — we already required caps / actor above
    if descriptor.approval_policy == APPROVAL_POLICY_REQUIRED and descriptor.read_only is False:
        # Soft signal; gateway/HITL still owns hard gate
        return AuthorizationDecision(
            True,
            "approval_required",
            required=required,
            provided=tuple(sorted(provided)),
        )

    return AuthorizationDecision(
        True, "allow", required=required, provided=tuple(sorted(provided))
    )
