import re

from agents.validators.models import (
    FACT_HEAVY_CATEGORIES,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    ValidationResult,
)
from security.redaction import redact
from tools.evidence import extract_claims, match_claim
from tools.gateway import SearchTimeoutError
from tools.models import (
    EVIDENCE_CONTRADICTED,
    EVIDENCE_SUPPORTED,
    MAX_FACT_CLAIMS,
    MAX_SEARCH_RESULTS_PER_CLAIM,
)
from tools.search.http_provider import SearchUnavailableError


SOURCE_URL_RE = re.compile(r"https?://|www\.", re.I)
SOURCE_MARKER_RE = re.compile(r"\b(?:source|источник)\b", re.I)
SKIP_EXTERNAL_CATEGORIES = frozenset({"strategy", "critique", "technical"})


class FactValidator:
    """
    Factual validation. External search goes only through ToolGateway.
    """

    def __init__(self, gateway=None, observability=None):
        self.name = "FactValidator"
        self.gateway = gateway
        self.observability = observability

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

    def _eligible(self, category: str | None, claims: tuple[str, ...]) -> bool:
        resolved = (category or "").strip()
        if resolved in FACT_HEAVY_CATEGORIES:
            return True
        if resolved == "general" and claims:
            return True
        return False

    def _unknown(self, *, reason, issues, claims_present, sources_present, category, extra=None):
        evidence = {
            "claims_present": claims_present,
            "sources_present": sources_present,
            "evidence_available": False,
            "category": category or "",
        }
        if extra:
            evidence.update(extra)
        return ValidationResult(
            validator_id="fact",
            status=STATUS_UNKNOWN,
            score=0.0,
            issues=tuple(redact(item) for item in issues),
            evidence=evidence,
            reason=reason,
        )

    async def validate(self, expert_answers: dict, *, category: str | None = None):
        result = await self._validate_impl(expert_answers, category=category)
        obs = self.observability
        if obs is None and self.gateway is not None:
            obs = getattr(self.gateway, "observability", None)
        if obs is not None:
            from observability.helpers import safe_emit

            safe_emit(
                obs,
                "validation.completed",
                context=obs.create_context(),
                component="validation",
                status=getattr(result, "status", ""),
                metadata={
                    "validator_type": "fact",
                    "pass": getattr(result, "status", "") == STATUS_PASS,
                    "confidence": getattr(result, "score", None),
                    "reason_code_count": 1 if getattr(result, "reason", None) else 0,
                },
            )
        return result

    async def _validate_impl(self, expert_answers: dict, *, category: str | None = None):
        claims_present = self._claims_present(expert_answers)
        sources_present = self._sources_present(expert_answers)
        resolved = (category or "").strip()
        texts = [str(answer or "") for answer in (expert_answers or {}).values()]
        claims = extract_claims(texts, limit=MAX_FACT_CLAIMS)

        if resolved in SKIP_EXTERNAL_CATEGORIES or not self._eligible(resolved, claims):
            issues = ["no_external_evidence"]
            if sources_present:
                issues.append("sources_mentioned_unverified")
            return self._unknown(
                reason="no_external_evidence",
                issues=issues,
                claims_present=claims_present,
                sources_present=sources_present,
                category=resolved,
            )

        if self.gateway is None:
            return self._unknown(
                reason="no_external_evidence",
                issues=["no_external_evidence"],
                claims_present=claims_present,
                sources_present=sources_present,
                category=resolved,
            )

        if not claims:
            return self._unknown(
                reason="no_extractable_claims",
                issues=["no_extractable_claims"],
                claims_present=claims_present,
                sources_present=sources_present,
                category=resolved,
            )

        evidence_rows = []
        try:
            if hasattr(self.gateway, "reset_budget"):
                self.gateway.reset_budget()
            for claim in claims:
                query = redact(claim).strip()
                if not query or query == "[REDACTED]":
                    continue
                rows = await self.gateway.search(
                    query,
                    max_results=MAX_SEARCH_RESULTS_PER_CLAIM,
                )
                evidence_rows.append(match_claim(claim, rows))
        except SearchTimeoutError:
            return self._unknown(
                reason="external_evidence_timeout",
                issues=["external_evidence_timeout"],
                claims_present=claims_present,
                sources_present=sources_present,
                category=resolved,
                extra={"evidence_available": False},
            )
        except SearchUnavailableError:
            return self._unknown(
                reason="external_evidence_unavailable",
                issues=["external_evidence_unavailable"],
                claims_present=claims_present,
                sources_present=sources_present,
                category=resolved,
            )

        if not evidence_rows:
            return self._unknown(
                reason="insufficient_evidence",
                issues=["insufficient_evidence"],
                claims_present=claims_present,
                sources_present=sources_present,
                category=resolved,
            )
        return self._aggregate(
            evidence_rows,
            claims_present=claims_present,
            sources_present=sources_present,
            category=resolved,
        )

    def _aggregate(self, evidence_rows, *, claims_present, sources_present, category):
        statuses = [row.status for row in evidence_rows]
        reasons = [row.reason for row in evidence_rows]
        extra = {
            "evidence_available": True,
            "claim_count": len(evidence_rows),
            "evidence_statuses": tuple(statuses),
            "supporting_source_count": sum(len(row.supporting_sources) for row in evidence_rows),
        }
        if EVIDENCE_CONTRADICTED in statuses:
            return ValidationResult(
                validator_id="fact",
                status=STATUS_FAIL,
                score=0.2,
                issues=("contradicting_evidence",),
                evidence={
                    "claims_present": claims_present,
                    "sources_present": sources_present,
                    "category": category or "",
                    **extra,
                },
                reason="contradicting_evidence",
            )
        if EVIDENCE_SUPPORTED in statuses:
            return ValidationResult(
                validator_id="fact",
                status=STATUS_PASS,
                score=0.7,
                issues=(),
                evidence={
                    "claims_present": claims_present,
                    "sources_present": sources_present,
                    "category": category or "",
                    **extra,
                },
                reason="independent_supporting_sources",
            )
        reason = reasons[0] if reasons else "insufficient_evidence"
        status = STATUS_WARN if reason == "single_source_insufficient" else STATUS_UNKNOWN
        return ValidationResult(
            validator_id="fact",
            status=status if status == STATUS_WARN else STATUS_UNKNOWN,
            score=0.2 if status == STATUS_WARN else 0.0,
            issues=(redact(reason),),
            evidence={
                "claims_present": claims_present,
                "sources_present": sources_present,
                "category": category or "",
                **extra,
            },
            reason=reason,
        )
