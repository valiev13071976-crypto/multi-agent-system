from tools.models import (
    EvidenceResult,
    SearchResult,
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_LEVELS,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    ToolDescriptor,
    ToolRequest,
    ToolResult,
    ToolUsageRecord,
)
from tools.search.null_provider import NullSearchProvider
from tools.url_safety import is_safe_http_url, validate_http_url, validate_redirect

__all__ = [
    "EvidenceResult",
    "NullSearchProvider",
    "SearchResult",
    "SearchTimeoutError",
    "TOOL_TRUST_INTERNAL_SAFE",
    "TOOL_TRUST_LEVELS",
    "TOOL_TRUST_PRIVILEGED",
    "TOOL_TRUST_READ_ONLY_EXTERNAL",
    "TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE",
    "TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE",
    "ToolDescriptor",
    "ToolGateway",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "ToolUsageRecord",
    "is_safe_http_url",
    "validate_http_url",
    "validate_redirect",
]


def __getattr__(name: str):
    # Lazy exports avoid circular imports: autonomy.policy → tools.models → tools.__init__.
    if name == "ToolGateway" or name == "SearchTimeoutError":
        from tools.gateway import SearchTimeoutError, ToolGateway

        return ToolGateway if name == "ToolGateway" else SearchTimeoutError
    if name == "ToolRegistry":
        from tools.registry import ToolRegistry

        return ToolRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
