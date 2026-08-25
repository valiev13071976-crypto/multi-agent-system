"""GitHub issue-label write adapter. Disabled unless explicitly configured."""

from side_effects.github.adapter import GitHubIssueLabelAdapter
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.github.models import GITHUB_TOOL_ID, OP_ENSURE_ABSENT, OP_ENSURE_PRESENT
from side_effects.github.transport import FakeGitHubTransport, GitHubHttpTransport

__all__ = [
    "GITHUB_TOOL_ID",
    "GitHubIssueLabelAdapter",
    "GitHubWriteAdapterConfig",
    "FakeGitHubTransport",
    "GitHubHttpTransport",
    "OP_ENSURE_ABSENT",
    "OP_ENSURE_PRESENT",
]
