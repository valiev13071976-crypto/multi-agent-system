"""Conservative durable knowledge write eligibility."""

from __future__ import annotations

from knowledge.models import (
    TRUST_DOCUMENT,
    TRUST_OPERATOR,
    TRUST_READ_ONLY_EXTERNAL,
    TRUST_SYSTEM,
    TRUST_UNVERIFIED,
    TRUST_VALIDATED_INTERNAL,
)


class KnowledgeWritePolicy:
    policy_version = "1.0.0"

    HIGH_TRUST = frozenset(
        {TRUST_SYSTEM, TRUST_OPERATOR, TRUST_VALIDATED_INTERNAL}
    )
    NEVER_AUTO_PROMOTE = frozenset({TRUST_UNVERIFIED, TRUST_READ_ONLY_EXTERNAL})

    def allow_persist(
        self,
        *,
        trust_level: str,
        validated: bool = False,
        contains_secret: bool = False,
        contains_policy_instruction: bool = False,
    ) -> bool:
        if contains_secret or contains_policy_instruction:
            return False
        if trust_level in self.NEVER_AUTO_PROMOTE and not validated:
            return False
        if trust_level == TRUST_DOCUMENT:
            return True
        if trust_level in self.HIGH_TRUST:
            return True
        return bool(validated)

    def can_promote_to_trusted(self, *, trust_level: str, validated: bool) -> bool:
        if trust_level in self.NEVER_AUTO_PROMOTE and not validated:
            return False
        return validated or trust_level in self.HIGH_TRUST


def knowledge_policy_snapshot() -> dict:
    return {
        "knowledge_policy_version": "1.0.0",
        "auto_persist_unverified": False,
        "external_vector_db": False,
        "network_on_startup": False,
        "rules": [
            "scoped_access_only",
            "no_arbitrary_url_query",
            "external_via_tool_gateway_only",
            "untrusted_default",
            "no_silent_conflict_merge",
            "no_auto_promote_unverified",
        ],
    }
