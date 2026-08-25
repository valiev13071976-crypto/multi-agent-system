"""Conservative auto-memory write eligibility."""

from __future__ import annotations

from memory.models import (
    MEMORY_PROCEDURAL,
    MEMORY_SEMANTIC,
    SOURCE_EXTERNAL,
    SOURCE_OPERATOR,
    SOURCE_SYSTEM,
    SOURCE_TOOL_RESULT,
    SOURCE_USER_INPUT,
    SOURCE_WORKFLOW_RESULT,
)


class MemoryWritePolicy:
    """Deterministic eligibility — default conservative, no LLM."""

    policy_version = "1.0.0"

    ELIGIBLE_SOURCES = frozenset(
        {
            SOURCE_OPERATOR,
            SOURCE_SYSTEM,
            SOURCE_WORKFLOW_RESULT,
        }
    )
    BLOCKED_SOURCES_FOR_TRUSTED = frozenset(
        {
            SOURCE_EXTERNAL,
            SOURCE_USER_INPUT,
            SOURCE_TOOL_RESULT,
        }
    )

    def allow_auto_store(
        self,
        *,
        memory_type: str,
        source_type: str,
        validated: bool = False,
        contains_policy_or_secret_instruction: bool = False,
    ) -> bool:
        if contains_policy_or_secret_instruction:
            return False
        if memory_type in {MEMORY_SEMANTIC, MEMORY_PROCEDURAL}:
            if source_type in self.BLOCKED_SOURCES_FOR_TRUSTED and not validated:
                return False
            if source_type == SOURCE_WORKFLOW_RESULT and not validated:
                return False
            return source_type in self.ELIGIBLE_SOURCES or (
                source_type == SOURCE_WORKFLOW_RESULT and validated
            )
        # episodic / working_reference: still require non-blocked explicit ingest
        return source_type in self.ELIGIBLE_SOURCES | {SOURCE_TOOL_RESULT} and not (
            contains_policy_or_secret_instruction
        )

    def is_poisoning_attempt(self, content: str) -> bool:
        lowered = str(content or "").lower()
        needles = (
            "ignore security",
            "ignore all previous",
            "bypass autonomy",
            "execute tool without",
            "disable hitl",
            "grant all capabilities",
        )
        return any(n in lowered for n in needles)
