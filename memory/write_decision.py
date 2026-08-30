"""Controlled memory write decision flow (Block 8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from memory.models import MemoryIngestRequest, MemoryScope
from memory.write_policy import MemoryWritePolicy

DECISION_ALLOW = "ALLOW"
DECISION_DENY = "DENY"
DECISION_REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
DECISION_EPHEMERAL_ONLY = "EPHEMERAL_ONLY"


@dataclass(frozen=True)
class MemoryWriteRequest:
    scope: MemoryScope
    ingest: MemoryIngestRequest
    explicit_user_authorized: bool = False
    model_suggestion: bool = False
    retrieved_content: bool = False
    policy_version: str = "1.0.0"
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "metadata_safe", MappingProxyType(sanitize_metadata(self.metadata_safe or {}))
        )


@dataclass(frozen=True)
class MemoryWriteDecision:
    decision: str
    reason: str
    policy_version: str
    allow_durable: bool
    requires_confirmation: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "metadata_safe", MappingProxyType(sanitize_metadata(self.metadata_safe or {}))
        )


_SECRET_NEEDLES = (
    "sk-",
    "ghp_",
    "api_key",
    "password",
    "Bearer ",
    "Authorization:",
)


class MemoryWriteGovernor:
    def __init__(self, write_policy: MemoryWritePolicy | None = None):
        self.write_policy = write_policy or MemoryWritePolicy()

    def evaluate(self, request: MemoryWriteRequest) -> MemoryWriteDecision:
        content = str(request.ingest.content or "")
        pv = self.write_policy.policy_version

        if self._looks_like_secret(content):
            return MemoryWriteDecision(
                decision=DECISION_DENY,
                reason="MEMORY_SECRET_REJECTED",
                policy_version=pv,
                allow_durable=False,
            )

        if request.retrieved_content and not request.explicit_user_authorized:
            return MemoryWriteDecision(
                decision=DECISION_DENY,
                reason="retrieved_content_not_authoritative",
                policy_version=pv,
                allow_durable=False,
            )

        if request.model_suggestion and not request.explicit_user_authorized:
            return MemoryWriteDecision(
                decision=DECISION_REQUIRE_CONFIRMATION,
                reason="model_suggestion_requires_confirmation",
                policy_version=pv,
                allow_durable=False,
                requires_confirmation=True,
            )

        poisoning = self.write_policy.is_poisoning_attempt(content)
        if poisoning:
            return MemoryWriteDecision(
                decision=DECISION_DENY,
                reason="poisoning_denied",
                policy_version=pv,
                allow_durable=False,
            )

        if request.explicit_user_authorized:
            return MemoryWriteDecision(
                decision=DECISION_ALLOW,
                reason="explicit_user_authorized",
                policy_version=pv,
                allow_durable=True,
            )

        if request.ingest.memory_type in {"working_reference", "episodic"}:
            return MemoryWriteDecision(
                decision=DECISION_EPHEMERAL_ONLY,
                reason="ephemeral_scope",
                policy_version=pv,
                allow_durable=False,
            )

        return MemoryWriteDecision(
            decision=DECISION_DENY,
            reason="MEMORY_WRITE_DENIED",
            policy_version=pv,
            allow_durable=False,
        )

    def _looks_like_secret(self, content: str) -> bool:
        lowered = content.lower()
        if any(n.lower() in lowered for n in _SECRET_NEEDLES):
            return True
        import re

        return bool(re.search(r"(?i)\b(api[_-]?key|password|secret)\b\s*[:=]", content))
