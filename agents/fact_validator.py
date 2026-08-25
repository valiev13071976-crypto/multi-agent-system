import re

from agents.validators.models import STATUS_UNKNOWN, ValidationResult
from security.redaction import redact


SOURCE_URL_RE = re.compile(r"https?://|www\.", re.I)
SOURCE_MARKER_RE = re.compile(r"\b(?:source|источник)\b", re.I)


class FactValidator:
    """
    P3A factual validation without external evidence.
    Does not claim facts are verified.
    """

    def __init__(self):
        self.name = "FactValidator"

    def _sources_present(self, experts: dict) -> bool:
        for answer in (experts or {}).values():
            text = str(answer or "")
            if SOURCE_URL_RE.search(text) or SOURCE_MARKER_RE.search(text):
                return True
        return False

    def _claims_present(self, experts: dict) -> bool:
        for answer in (experts or {}).values():
            if len(str(answer or "").strip()) >= 8:
                return True
        return False

    async def validate(self, expert_answers: dict, *, category: str | None = None):
        claims_present = self._claims_present(expert_answers)
        sources_present = self._sources_present(expert_answers)
        issues = ["no_external_evidence"]
        if sources_present:
            issues.append("sources_mentioned_unverified")
        return ValidationResult(
            validator_id="fact",
            status=STATUS_UNKNOWN,
            score=0.0,
            issues=tuple(redact(item) for item in issues),
            evidence={
                "claims_present": claims_present,
                "sources_present": sources_present,
                "evidence_available": False,
                "category": category or "",
            },
            reason="no_external_evidence",
        )
