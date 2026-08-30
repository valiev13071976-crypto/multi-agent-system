import re
import uuid

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

VALIDATOR_ID = "fact"
VALIDATOR_VERSION = "1.0.0"


class FactValidator:
    """
    Factual validation. External search goes only through ToolGateway.
    """

    validator_version = VALIDATOR_VERSION

    def __init__(self, gateway=None, observability=None):
        self.name = "FactValidator"
        self.gateway = gateway
        self.observability = observability
        self.validator_version = VALIDATOR_VERSION

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

    def _obs_runtime(self):
        obs = self.observability
        if obs is None and self.gateway is not None:
            obs = getattr(self.gateway, "observability", None)
        return obs

    def _resolve_obs_context(
        self,
        *,
        parent_context=None,
        envelope=None,
        task_id: str = "",
        workflow_id: str = "",
        tenant_id: str = "",
        actor_ref: str = "",
        correlation_id: str | None = None,
    ):
        """Prefer parent/envelope lineage; never invent competing root when parent exists."""
        obs = self._obs_runtime()
        if obs is None:
            return None

        if parent_context is not None:
            return obs.child_span(
                parent_context,
                workflow_id=workflow_id or None,
                task_id=task_id or None,
                tenant_id=tenant_id or None,
                actor_ref=actor_ref or None,
            )

        if envelope is not None:
            env_workflow = str(getattr(envelope, "workflow_id", "") or "")
            env_task = str(getattr(envelope, "task_id", "") or task_id or "")
            env_tenant = str(getattr(envelope, "tenant_id", "") or "")
            env_actor = str(getattr(envelope, "actor_ref", "") or "")
            existing = (
                obs.context_for_workflow(env_workflow) if env_workflow else None
            )
            if existing is not None:
                return existing.child(
                    task_id=env_task or existing.task_id,
                    actor_ref=env_actor or None,
                    tenant_id=env_tenant or None,
                )
            from observability.context import ObservabilityContext

            return ObservabilityContext(
                correlation_id=str(envelope.correlation_id),
                trace_id=str(envelope.trace_id),
                span_id=str(uuid.uuid4()),
                parent_span_id=None,
                workflow_id=env_workflow,
                task_id=env_task,
                actor_ref=env_actor,
                tenant_id=env_tenant,
            )

        resolved_workflow = str(workflow_id or "")
        if resolved_workflow:
            existing = obs.context_for_workflow(resolved_workflow)
            if existing is not None:
                return existing.child(
                    task_id=task_id or existing.task_id,
                    actor_ref=actor_ref or None,
                    tenant_id=tenant_id or None,
                )

        # Legacy callers without parent/envelope.
        return obs.create_context(
            correlation_id=correlation_id,
            workflow_id=resolved_workflow,
            task_id=str(task_id or ""),
            actor_ref=str(actor_ref or ""),
            tenant_id=str(tenant_id or ""),
        )

    def _emit_validation_completed(
        self,
        result,
        *,
        envelope=None,
        parent_context=None,
        task_id: str = "",
        workflow_id: str = "",
        tenant_id: str = "",
        actor_ref: str = "",
        request_id: str | None = None,
    ) -> None:
        obs = self._obs_runtime()
        if obs is None:
            return
        from observability.helpers import safe_emit

        if envelope is not None:
            task_id = str(getattr(envelope, "task_id", "") or task_id or "")
            workflow_id = str(getattr(envelope, "workflow_id", "") or "")
            tenant_id = str(getattr(envelope, "tenant_id", "") or "")
            actor_ref = str(getattr(envelope, "actor_ref", "") or "")
            request_id = str(getattr(envelope, "request_id", "") or request_id or "") or None

        context = self._resolve_obs_context(
            parent_context=parent_context,
            envelope=envelope,
            task_id=task_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            actor_ref=actor_ref,
            correlation_id=request_id,
        )
        safe_emit(
            obs,
            "validation.completed",
            context=context,
            component="validation",
            status=getattr(result, "status", ""),
            metadata={
                "validator_type": "fact",
                "pass": getattr(result, "status", "") == STATUS_PASS,
                "confidence": getattr(result, "score", None),
                "reason_code_count": 1 if getattr(result, "reason", None) else 0,
            },
        )

    async def validate(
        self,
        expert_answers: dict,
        *,
        category: str | None = None,
        envelope=None,
        parent_context=None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        tenant_id: str | None = None,
        actor_ref: str | None = None,
        request_id: str | None = None,
    ):
        result = await self._validate_impl(expert_answers, category=category)
        self._emit_validation_completed(
            result,
            envelope=envelope,
            parent_context=parent_context,
            task_id=str(task_id or ""),
            workflow_id=str(workflow_id or ""),
            tenant_id=str(tenant_id or ""),
            actor_ref=str(actor_ref or ""),
            request_id=request_id,
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
