from autonomy.models import utc_now
from side_effects.activation import (
    ACTIVATION_BLOCKED,
    ACTIVATION_CONFIGURED,
    ACTIVATION_DISABLED,
    ACTIVATION_DRY_RUN,
    ACTIVATION_ERROR,
    ACTIVATION_READY,
    GitHubReadinessResult,
    OperationalActivationDecision,
    PURPOSE_DRY_RUN,
    PURPOSE_MUTATE,
    PURPOSE_PROBE,
    PURPOSE_ROLLBACK,
    READINESS_BLOCKED,
    READINESS_PARTIAL,
    READINESS_READY,
    SideEffectRuntimeHealth,
    WRITE_PERMISSION_DENIED,
)
from side_effects.github.models import GITHUB_TOOL_ID, parse_github_label_resource
from side_effects.github.readiness import GitHubReadinessProbe
from side_effects.models import (
    EVENT_ADAPTER_BLOCKED,
    EVENT_ADAPTER_CONFIGURED,
    EVENT_ADAPTER_DISABLED,
    EVENT_ADAPTER_DRY_RUN,
    EVENT_ADAPTER_READY,
    EVENT_KILL_SWITCH_BLOCKED,
    EVENT_READINESS_PROBE_FAILED,
    EVENT_READINESS_PROBE_PASSED,
    EVENT_READINESS_PROBE_STARTED,
)


class GitHubWriteActivationService:
    """Operational activation only. Does not own Autonomy, HITL, or idempotency."""

    def __init__(
        self,
        *,
        config,
        transport=None,
        audit=None,
        registered: bool = False,
        composition_error: str | None = None,
    ):
        self._config = config
        self._transport = transport
        self._audit = audit
        self._registered = bool(registered)
        self._composition_error = composition_error
        self._readiness: GitHubReadinessResult | None = None
        self._state = self._derive_state()
        self._emit_state()

    def _derive_state(self) -> str:
        if self._composition_error:
            return ACTIVATION_ERROR
        if not self._config.enabled:
            return ACTIVATION_DISABLED
        if self._config.kill_switch:
            return ACTIVATION_BLOCKED
        if self._config.dry_run:
            return ACTIVATION_DRY_RUN
        if self._readiness is None:
            return ACTIVATION_CONFIGURED
        if self._readiness.status in {READINESS_BLOCKED}:
            return ACTIVATION_BLOCKED
        if self._readiness.write_permission_status == WRITE_PERMISSION_DENIED:
            return ACTIVATION_BLOCKED
        return ACTIVATION_READY

    def _emit_state(self) -> None:
        if self._audit is None:
            return
        mapping = {
            ACTIVATION_DISABLED: EVENT_ADAPTER_DISABLED,
            ACTIVATION_CONFIGURED: EVENT_ADAPTER_CONFIGURED,
            ACTIVATION_DRY_RUN: EVENT_ADAPTER_DRY_RUN,
            ACTIVATION_READY: EVENT_ADAPTER_READY,
            ACTIVATION_BLOCKED: EVENT_ADAPTER_BLOCKED,
            ACTIVATION_ERROR: EVENT_ADAPTER_BLOCKED,
        }
        event = mapping.get(self._state)
        if event:
            self._audit.record(
                event,
                tool_id=GITHUB_TOOL_ID,
                reason_code=self._state_reason(),
                metadata={"activation_state": self._state},
            )

    def _state_reason(self) -> str:
        if self._composition_error:
            return self._composition_error
        if not self._config.enabled:
            return "github_write_adapter_disabled"
        if self._config.kill_switch:
            return "github_write_kill_switch_active"
        if self._config.dry_run:
            return "github_write_dry_run_active"
        if self._readiness is None:
            return "github_readiness_pending"
        return self._readiness.reason_code

    @property
    def state(self) -> str:
        return self._derive_state()

    @property
    def readiness(self) -> GitHubReadinessResult | None:
        return self._readiness

    def health(self) -> SideEffectRuntimeHealth:
        readiness = self._readiness
        return SideEffectRuntimeHealth(
            adapter_id=GITHUB_TOOL_ID,
            activation_state=self.state,
            configured=bool(self._config.enabled and self._config.allowed_repositories),
            registered=self._registered,
            dry_run=bool(self._config.dry_run),
            kill_switch=bool(self._config.kill_switch),
            readiness_status=None if readiness is None else readiness.status,
            last_probe_at=None if readiness is None else readiness.checked_at,
            reason_code=self._state_reason(),
        )

    async def refresh(self, *, now=None) -> GitHubReadinessResult:
        if self._audit is not None:
            self._audit.record(
                EVENT_READINESS_PROBE_STARTED,
                tool_id=GITHUB_TOOL_ID,
                reason_code="probe_started",
            )
        if self._transport is None:
            result = GitHubReadinessResult(
                status=READINESS_BLOCKED,
                authenticated=False,
                repository_accessible=False,
                write_permission_status="unknown",
                checked_at=now or utc_now(),
                expires_at=None,
                repository_results=(),
                reason_code="github_write_secret_missing",
            )
            self._readiness = result
            if self._audit is not None:
                self._audit.record(
                    EVENT_READINESS_PROBE_FAILED,
                    tool_id=GITHUB_TOOL_ID,
                    reason_code=result.reason_code,
                )
            return result
        probe = GitHubReadinessProbe(self._transport, timeout_seconds=self._config.timeout_seconds)
        result = await probe.probe(
            self._config.allowed_repositories,
            ttl_seconds=self._config.readiness_ttl_seconds,
            now=now,
        )
        self._readiness = result
        if self._audit is not None:
            event = (
                EVENT_READINESS_PROBE_PASSED
                if result.status in {READINESS_READY, READINESS_PARTIAL}
                else EVENT_READINESS_PROBE_FAILED
            )
            self._audit.record(
                event,
                tool_id=GITHUB_TOOL_ID,
                reason_code=result.reason_code,
                metadata={"readiness_status": result.status},
            )
        self._state = self._derive_state()
        return result

    def evaluate(
        self, action, adapter_descriptor, *, purpose: str = PURPOSE_MUTATE, now=None
    ) -> OperationalActivationDecision:
        stamp = now or utc_now()
        if purpose == PURPOSE_PROBE:
            return OperationalActivationDecision(
                allowed=True, dry_run=False, blocked=False, reason_code="probe_allowed", checked_at=stamp
            )
        if self._composition_error:
            return OperationalActivationDecision(
                allowed=False,
                dry_run=False,
                blocked=True,
                reason_code=self._composition_error,
                checked_at=stamp,
            )
        if not self._config.enabled:
            return OperationalActivationDecision(
                allowed=False,
                dry_run=False,
                blocked=True,
                reason_code="github_write_adapter_disabled",
                checked_at=stamp,
            )
        if purpose in {PURPOSE_MUTATE, PURPOSE_ROLLBACK} and self._config.kill_switch:
            if self._audit is not None:
                self._audit.record(
                    EVENT_KILL_SWITCH_BLOCKED,
                    tool_id=GITHUB_TOOL_ID,
                    action_id=getattr(action, "action_id", None),
                    reason_code="github_write_kill_switch_active",
                )
            return OperationalActivationDecision(
                allowed=False,
                dry_run=False,
                blocked=True,
                reason_code="github_write_kill_switch_active",
                checked_at=stamp,
            )
        repo_decision = self._repository_decision(action, stamp, purpose)
        if repo_decision is not None:
            return repo_decision
        if purpose == PURPOSE_DRY_RUN:
            return OperationalActivationDecision(
                allowed=True,
                dry_run=True,
                blocked=False,
                reason_code="github_write_dry_run_active",
                checked_at=stamp,
            )
        if self._config.dry_run:
            return OperationalActivationDecision(
                allowed=False,
                dry_run=True,
                blocked=False,
                reason_code="github_write_dry_run_active",
                checked_at=stamp,
            )
        return OperationalActivationDecision(
            allowed=True,
            dry_run=False,
            blocked=False,
            reason_code="activation_ready",
            checked_at=stamp,
        )

    def _repository_decision(self, action, stamp, purpose: str):
        resource = str(getattr(action, "resource", "") or "")
        try:
            target = parse_github_label_resource(resource)
        except Exception:
            target = None
        if target is not None and not self._config.allows(target.owner, target.repo):
            return OperationalActivationDecision(
                allowed=False,
                dry_run=False,
                blocked=True,
                reason_code="github_repository_not_allowed",
                checked_at=stamp,
            )
        if purpose == PURPOSE_DRY_RUN:
            return None
        if purpose in {PURPOSE_MUTATE, PURPOSE_ROLLBACK}:
            if self._config.require_probe_success:
                if self._readiness is None:
                    return OperationalActivationDecision(
                        allowed=False,
                        dry_run=False,
                        blocked=True,
                        reason_code="github_readiness_required",
                        checked_at=stamp,
                    )
                if self._readiness.expires_at is not None and stamp >= self._readiness.expires_at:
                    return OperationalActivationDecision(
                        allowed=False,
                        dry_run=False,
                        blocked=True,
                        reason_code="github_readiness_expired",
                        checked_at=stamp,
                    )
                if self._readiness.write_permission_status == WRITE_PERMISSION_DENIED:
                    return OperationalActivationDecision(
                        allowed=False,
                        dry_run=False,
                        blocked=True,
                        reason_code="github_permission_denied",
                        checked_at=stamp,
                    )
                if self._readiness.status == READINESS_BLOCKED:
                    return OperationalActivationDecision(
                        allowed=False,
                        dry_run=False,
                        blocked=True,
                        reason_code=self._readiness.reason_code,
                        checked_at=stamp,
                    )
                if target is not None:
                    row = self._readiness.for_repository(target.owner, target.repo)
                    if row is None or not row.accessible:
                        return OperationalActivationDecision(
                            allowed=False,
                            dry_run=False,
                            blocked=True,
                            reason_code="github_repository_inaccessible",
                            checked_at=stamp,
                        )
        return None
