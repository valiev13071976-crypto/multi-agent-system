import uuid

from autonomy.approval import ApprovalService
from autonomy.capabilities import (
    CapabilitySet,
    required_capabilities_for,
    scope_mismatch,
)
from autonomy.errors import IdempotencyConflictError
from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import (
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    DECISION_REVIEW_AFTER,
    DEFAULT_AUTONOMY_LEVEL,
    IDEMPOTENCY_UNCERTAIN,
    PROTECTED_IDEMPOTENCY_TYPES,
    ProposedAction,
    AutonomyDecision,
    SIDE_EFFECT_TYPES,
    utc_now,
)
from autonomy.policy import ApprovalPolicy
from autonomy.risk import ActionRiskClassifier
from autonomy.store import (
    InMemoryCapabilityTokenStore,
)
from autonomy.tokens import (
    SignedCapabilityToken,
    TokenSigner,
    token_public_claims,
    validate_token,
)
from tools.models import TOOL_TRUST_LEVELS


def queue_side_effect_permitted(decision: AutonomyDecision | None) -> bool:
    """TaskQueue must not bypass this. Only ALLOW may reach a future write executor."""
    return decision is not None and decision.decision == DECISION_ALLOW


class AutonomyGate:
    """Single policy owner for side-effect actions. No tool execution."""

    def __init__(
        self,
        *,
        policy: ApprovalPolicy | None = None,
        classifier: ActionRiskClassifier | None = None,
        approvals: ApprovalService | None = None,
        idempotency: IdempotencyRegistry | None = None,
        tokens=None,
        signer: TokenSigner | None = None,
        autonomy_level: str = DEFAULT_AUTONOMY_LEVEL,
    ):
        self.policy = policy or ApprovalPolicy()
        self.classifier = classifier or ActionRiskClassifier()
        self.approvals = approvals or ApprovalService()
        self.idempotency = idempotency or IdempotencyRegistry()
        self.tokens = tokens or InMemoryCapabilityTokenStore()
        self.signer = signer
        self.autonomy_level = autonomy_level
        self.last_decision: AutonomyDecision | None = None
        self.observability = None
        self.obs_context = None

    def evaluate(
        self,
        action: ProposedAction,
        *,
        token: SignedCapabilityToken | None = None,
        capabilities: CapabilitySet | None = None,
        approval=None,
        autonomy_level: str | None = None,
        now=None,
    ) -> AutonomyDecision:
        stamp = now or utc_now()
        level = autonomy_level or self.autonomy_level
        meta = dict(action.metadata)
        reversible = bool(meta.get("reversible", False))
        required = required_capabilities_for(
            action.action_type, action.requested_capabilities
        )
        unknown_cap = action.action_type in SIDE_EFFECT_TYPES and not required
        risk = action.risk_class or self.classifier.classify(
            action.action_type,
            action.operation,
            action.resource,
            meta,
        )

        cap_valid = False
        cap_reason = "capability_missing"
        token_reason = None
        scope_valid = True
        scope_reason = None

        if token is not None:
            if self.signer is None:
                token_reason = "token_invalid"
                cap_valid = set(required) <= set(token.token.capabilities)
                cap_reason = None if cap_valid else "capability_missing"
            else:
                revoked = set()
                if hasattr(self.tokens, "is_revoked"):
                    if self.tokens.is_revoked(token.token.token_id):
                        revoked.add(token.token.token_id)
                token_reason = validate_token(
                    token,
                    action=action,
                    required=required,
                    signer=self.signer,
                    now=stamp,
                    revoked_ids=revoked,
                )
                if token_reason is None:
                    cap_valid = True
                    cap_reason = None
                elif token_reason == "capability_missing":
                    cap_valid = False
                    cap_reason = "capability_missing"
                else:
                    cap_valid = set(required) <= set(token.token.capabilities)
                    cap_reason = None if cap_valid else "capability_missing"
        elif capabilities is not None:
            if capabilities.expires_at is not None and capabilities.expires_at <= stamp:
                token_reason = "token_expired"
            elif not capabilities.has_all(required):
                cap_reason = "capability_missing"
            else:
                mismatch = scope_mismatch(capabilities.scope, action)
                if mismatch:
                    scope_valid = False
                    scope_reason = mismatch
                else:
                    cap_valid = True
                    cap_reason = None
        else:
            cap_reason = "capability_missing"

        idempotency_required = action.action_type in PROTECTED_IDEMPOTENCY_TYPES
        idempotency_ready = True
        idempotency_conflict = False
        idempotency_reason = None
        if idempotency_required:
            if not action.idempotency_key:
                idempotency_ready = False
            else:
                existing = self.idempotency.get(action.idempotency_key)
                if existing is not None:
                    if existing.state == IDEMPOTENCY_UNCERTAIN:
                        idempotency_conflict = True
                        idempotency_reason = "duplicate_uncertain"
                    elif existing.action_id != action.action_id:
                        idempotency_conflict = True
                        idempotency_reason = (
                            "duplicate_completed"
                            if existing.state == "completed"
                            else "duplicate_active"
                        )

        ctx = {
            "autonomy_level": level,
            "risk_class": risk,
            "tool_trust_level": action.tool_trust_level,
            "action_type": action.action_type,
            "capabilities_valid": cap_valid,
            "capability_reason": cap_reason,
            "token_reason": token_reason,
            "scope_valid": scope_valid,
            "scope_reason": scope_reason,
            "idempotency_ready": idempotency_ready,
            "idempotency_conflict": idempotency_conflict,
            "idempotency_reason": idempotency_reason,
            "reversible": reversible,
            "unknown_capability_requirement": unknown_cap,
            "unknown_destructive": bool(meta.get("unknown") and meta.get("destructive")),
            "approval_status": getattr(approval, "status", None),
        }
        decision_value, reason = self.policy.decide(ctx)

        if (
            decision_value
            in {DECISION_ALLOW, DECISION_REVIEW_AFTER, DECISION_REQUIRE_APPROVAL}
            and idempotency_required
            and action.idempotency_key
            and not idempotency_conflict
        ):
            existing = self.idempotency.get(action.idempotency_key)
            if existing is None:
                try:
                    self.idempotency.reserve(
                        action.idempotency_key,
                        action.action_id,
                        metadata={"action_id": action.action_id, "tool_id": action.tool_id},
                    )
                except IdempotencyConflictError as exc:
                    decision_value = DECISION_DENY
                    reason = exc.reason_code

        public_token = {}
        if token is not None:
            public_token = token_public_claims(token.token)

        decision = AutonomyDecision(
            decision_id=str(uuid.uuid4()),
            action_id=action.action_id,
            decision=decision_value,
            risk_class=risk,
            reason_code=reason,
            required_approval=decision_value == DECISION_REQUIRE_APPROVAL,
            capabilities_checked=required,
            idempotency_required=idempotency_required,
            idempotency_satisfied=bool(
                idempotency_ready and not idempotency_conflict
            ),
            tool_trust_level=action.tool_trust_level,
            timestamp=stamp,
            metadata={
                "workflow_id": action.workflow_id,
                "task_id": action.task_id,
                "tool_id": action.tool_id,
                "autonomy_level": level,
                "token_claims": public_token,
                "approval_status": getattr(approval, "status", None),
            },
        )
        blob = str(dict(decision.metadata))
        if token is not None and token.signature and token.signature in blob:
            raise RuntimeError("signature_leaked")
        self.last_decision = decision
        obs = getattr(self, "observability", None)
        if obs is not None:
            parent = getattr(self, "obs_context", None)
            span = obs.child_span(parent) if parent is not None else obs.create_context(
                workflow_id=action.workflow_id,
                task_id=action.task_id,
            )
            obs.emit(
                "autonomy.evaluated",
                context=span,
                component="autonomy_gate",
                status=decision.decision,
                risk=decision.risk_class,
                trust_level=decision.tool_trust_level,
                tool_id=action.tool_id,
                operation=action.operation,
                metadata={
                    "decision": decision.decision,
                    "risk_level": decision.risk_class,
                    "reason_code": decision.reason_code,
                    "required_capability_count": len(decision.capabilities_checked),
                },
                update_metrics=False,
            )
        return decision


def build_proposed_action(
    *,
    action_type: str,
    tool_id: str = "search",
    operation: str = "search",
    resource: str = "web",
    tool_trust_level: str = "READ_ONLY_EXTERNAL",
    requested_capabilities=None,
    idempotency_key: str | None = None,
    workflow_id: str = "wf-1",
    task_id: str = "task-1",
    metadata=None,
    risk_class: str | None = None,
    action_id: str | None = None,
) -> ProposedAction:
    meta = dict(metadata or {})
    classifier = ActionRiskClassifier()
    return ProposedAction(
        action_id=action_id or str(uuid.uuid4()),
        workflow_id=workflow_id,
        task_id=task_id,
        action_type=action_type,
        tool_id=tool_id,
        operation=operation,
        resource=resource,
        risk_class=risk_class
        or classifier.classify(action_type, operation, resource, meta),
        requested_capabilities=tuple(
            requested_capabilities
            if requested_capabilities is not None
            else required_capabilities_for(action_type)
        ),
        tool_trust_level=tool_trust_level,
        idempotency_key=idempotency_key,
        metadata=meta,
    )
