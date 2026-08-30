from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import utc_now


CAP_EXTERNAL_READ = "external_read"
CAP_EXTERNAL_WRITE = "external_write"
CAP_MESSAGE_SEND = "message_send"
CAP_CRM_READ = "crm_read"
CAP_CRM_WRITE = "crm_write"
CAP_FILESYSTEM_READ = "filesystem_read"
CAP_FILESYSTEM_WRITE = "filesystem_write"
CAP_CODE_EXECUTE = "code_execute"
CAP_SITE_READ = "site_read"
CAP_SITE_WRITE = "site_write"
CAP_PRICING_READ = "pricing_read"
CAP_PRICING_WRITE = "pricing_write"
CAP_PURCHASE = "purchase"
CAP_FINANCIAL_CHANGE = "financial_change"
CAP_PERMISSION_MANAGE = "permission_manage"
CAP_GITHUB_ISSUE_LABEL_WRITE = "github_issue_label_write"
CAP_EMAIL_READ = "email_read"
CAP_EMAIL_SEND = "email_send"
CAP_CALENDAR_READ = "calendar_read"
CAP_CALENDAR_WRITE = "calendar_write"
CAP_TELEGRAM_READ = "telegram_read"
CAP_TELEGRAM_SEND = "telegram_send"
CAP_DB_READ = "db_read"
CAP_DB_WRITE = "db_write"
CAP_BROWSER_READ = "browser_read"
CAP_BROWSER_WRITE = "browser_write"
CAP_IMAGE_GENERATE = "image_generate"
CAP_IMAGE_EDIT = "image_edit"
CAP_SCRAPE = "scrape"
CAP_SEO_READ = "seo_read"
CAP_SEO_WRITE = "seo_write"
CAP_MCP_INVOKE = "mcp_invoke"

CAPABILITIES = (
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CAP_MESSAGE_SEND,
    CAP_CRM_READ,
    CAP_CRM_WRITE,
    CAP_FILESYSTEM_READ,
    CAP_FILESYSTEM_WRITE,
    CAP_CODE_EXECUTE,
    CAP_SITE_READ,
    CAP_SITE_WRITE,
    CAP_PRICING_READ,
    CAP_PRICING_WRITE,
    CAP_PURCHASE,
    CAP_FINANCIAL_CHANGE,
    CAP_PERMISSION_MANAGE,
    CAP_GITHUB_ISSUE_LABEL_WRITE,
    CAP_EMAIL_READ,
    CAP_EMAIL_SEND,
    CAP_CALENDAR_READ,
    CAP_CALENDAR_WRITE,
    CAP_TELEGRAM_READ,
    CAP_TELEGRAM_SEND,
    CAP_DB_READ,
    CAP_DB_WRITE,
    CAP_BROWSER_READ,
    CAP_BROWSER_WRITE,
    CAP_IMAGE_GENERATE,
    CAP_IMAGE_EDIT,
    CAP_SCRAPE,
    CAP_SEO_READ,
    CAP_SEO_WRITE,
    CAP_MCP_INVOKE,
)

DEFAULT_REQUIRED = {
    "read": (CAP_EXTERNAL_READ,),
    "write": (CAP_EXTERNAL_WRITE,),
    "send_message": (CAP_MESSAGE_SEND,),
    "delete": (CAP_EXTERNAL_WRITE,),
    "purchase": (CAP_PURCHASE,),
    "financial_change": (CAP_FINANCIAL_CHANGE,),
    "permission_change": (CAP_PERMISSION_MANAGE,),
    "external_publish": (CAP_SITE_WRITE,),
    "execute_code": (CAP_CODE_EXECUTE,),
}


def _scope_map(value) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class CapabilityScope:
    tool_id: str | None = None
    operation: str | None = None
    resource_pattern: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "operation": self.operation,
            "resource_pattern": self.resource_pattern,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
        }

    def matches_resource(self, resource: str) -> bool:
        return matches_resource(self.resource_pattern, resource)


@dataclass(frozen=True)
class CapabilitySet:
    subject_id: str
    capabilities: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime | None = None
    scope: CapabilityScope = field(default_factory=CapabilityScope)
    issuer: str = "autonomy-gate"
    version: str = "1"

    def __post_init__(self):
        object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def has_all(self, required: tuple[str, ...] | list[str]) -> bool:
        have = set(self.capabilities)
        return set(required) <= have


def required_capabilities_for(action_type: str, requested=None) -> tuple[str, ...]:
    if requested:
        return tuple(requested)
    return DEFAULT_REQUIRED.get(action_type, ())


def matches_resource(pattern: str | None, resource: str) -> bool:
    if not pattern:
        return True
    if pattern == resource:
        return True
    if pattern.endswith("*"):
        return str(resource).startswith(pattern[:-1])
    return False


def scope_mismatch(scope: CapabilityScope, action) -> str | None:
    if scope.workflow_id and scope.workflow_id != action.workflow_id:
        return "scope_workflow_mismatch"
    if scope.task_id and scope.task_id != action.task_id:
        return "scope_task_mismatch"
    if scope.tool_id and scope.tool_id != action.tool_id:
        return "scope_tool_mismatch"
    if scope.operation and scope.operation != action.operation:
        return "scope_operation_mismatch"
    if scope.resource_pattern and not matches_resource(
        scope.resource_pattern, action.resource
    ):
        return "scope_resource_mismatch"
    return None
