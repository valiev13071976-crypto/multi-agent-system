"""GitHub Issue label adapter.

Minimum token permission: Issues Read and write.
Does not require Contents write.
Does not inspect token scopes via a destructive API call.
"""

from __future__ import annotations

import asyncio
import hashlib

from autonomy.capabilities import CAP_EXTERNAL_WRITE, CAP_GITHUB_ISSUE_LABEL_WRITE
from side_effects.errors import RollbackExecutionError, SideEffectExecutionError
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.github.errors import GitHubAdapterError
from side_effects.github.models import (
    GITHUB_OPERATIONS,
    GITHUB_TOOL_ID,
    OP_ENSURE_ABSENT,
    OP_ENSURE_PRESENT,
    RESOURCE_PREFIX,
    GitHubIssueLabelTarget,
    GitHubTargetError,
    label_present,
    parse_github_label_resource,
)
from side_effects.models import (
    ADAPTER_RECON_FAILED,
    ADAPTER_RECON_SUCCEEDED,
    ADAPTER_RECON_UNKNOWN,
    AdapterExecutionResult,
    AdapterReconciliationResult,
    RollbackResult,
    SideEffectToolDescriptor,
)
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


def _encode_rollback(*, prior_present: bool, changed: bool) -> str:
    return f"prior_present={int(prior_present)}:changed={int(changed)}"


def _parse_rollback(reference: str) -> tuple[bool, bool]:
    text = str(reference or "")
    prior = False
    changed = False
    for part in text.split(":"):
        if part.startswith("prior_present="):
            prior = part.split("=", 1)[1] == "1"
        elif part.startswith("changed="):
            changed = part.split("=", 1)[1] == "1"
    return prior, changed


class GitHubIssueLabelAdapter:
    """State-based ensure present/absent for one issue label. No generic HTTP API."""

    def __init__(self, *, config: GitHubWriteAdapterConfig, transport):
        if config.enabled and not config.allowed_repositories:
            from side_effects.github.errors import GitHubWriteConfigError

            raise GitHubWriteConfigError("github_allowlist_empty")
        self._config = config
        self._transport = transport
        self._descriptor = SideEffectToolDescriptor(
            tool_id=GITHUB_TOOL_ID,
            trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            capabilities_required=(CAP_EXTERNAL_WRITE, CAP_GITHUB_ISSUE_LABEL_WRITE),
            reversible=True,
            supports_idempotency=True,
            network_access=True,
            operations=GITHUB_OPERATIONS,
            resource_prefix=RESOURCE_PREFIX,
            supports_reconciliation=True,
            reconciliation_authoritative=True,
            not_found_is_authoritative_failure=False,
            idempotency_mode="state_reconciliation",
        )
        self.calls = 0
        self.rollback_calls = 0
        self.reconcile_calls = 0
        self.mutated = False
        self.received_idempotency_keys: list[str] = []

    @property
    def descriptor(self) -> SideEffectToolDescriptor:
        return self._descriptor

    @property
    def tool_id(self) -> str:
        return self._descriptor.tool_id

    @property
    def trust_level(self) -> str:
        return self._descriptor.trust_level

    @property
    def capabilities_required(self) -> tuple[str, ...]:
        return self._descriptor.capabilities_required

    @property
    def reversible(self) -> bool:
        return True

    def _target(self, action, context) -> GitHubIssueLabelTarget:
        try:
            target = parse_github_label_resource(str(action.resource or ""))
        except GitHubTargetError as exc:
            raise SideEffectExecutionError(str(exc.error_code)) from exc
        payload = dict(getattr(context, "payload", {}) or {})
        if any(callable(item) for item in payload.values()):
            raise SideEffectExecutionError("invalid_input")
        if any(isinstance(item, (dict, list, tuple, set)) for item in payload.values()):
            raise SideEffectExecutionError("invalid_input")
        allowed_keys = {"owner", "repo", "issue_number", "label"}
        extra = set(payload) - allowed_keys
        if extra:
            raise SideEffectExecutionError("invalid_input")
        if "owner" in payload and str(payload["owner"]) != target.owner:
            raise SideEffectExecutionError("github_target_mismatch")
        if "repo" in payload and str(payload["repo"]) != target.repo:
            raise SideEffectExecutionError("github_target_mismatch")
        if "issue_number" in payload and int(payload["issue_number"]) != target.issue_number:
            raise SideEffectExecutionError("github_target_mismatch")
        if "label" in payload and str(payload["label"]) != target.label:
            raise SideEffectExecutionError("github_target_mismatch")
        if not self._config.allows(target.owner, target.repo):
            raise SideEffectExecutionError("github_repository_not_allowed")
        return target

    def _remap_timeout(self, exc: GitHubAdapterError, *, after_mutation: bool) -> None:
        if exc.error_code != "github_timeout_uncertain":
            raise exc
        if after_mutation:
            if self.mutated:
                raise SideEffectExecutionError("external_verification_uncertain") from exc
            raise SideEffectExecutionError("external_write_timeout_uncertain") from exc
        raise SideEffectExecutionError("github_timeout_uncertain") from exc

    async def _labels(self, target: GitHubIssueLabelTarget, *, after_mutation: bool) -> tuple[str, ...]:
        try:
            return await self._transport.get_issue_labels(
                target.owner, target.repo, target.issue_number
            )
        except GitHubAdapterError as exc:
            if after_mutation and exc.error_code == "github_resource_not_found_or_inaccessible":
                raise SideEffectExecutionError("external_verification_uncertain") from exc
            self._remap_timeout(exc, after_mutation=after_mutation)
            raise

    async def _mutate(self, operation: str, target: GitHubIssueLabelTarget) -> None:
        self.mutated = True
        try:
            if operation == OP_ENSURE_PRESENT:
                await self._transport.add_label(
                    target.owner, target.repo, target.issue_number, target.label
                )
            else:
                try:
                    await self._transport.remove_label(
                        target.owner, target.repo, target.issue_number, target.label
                    )
                except GitHubAdapterError as exc:
                    if exc.error_code != "github_resource_not_found_or_inaccessible":
                        raise
        except GitHubAdapterError as exc:
            if exc.error_code == "github_timeout_uncertain":
                raise SideEffectExecutionError("external_write_timeout_uncertain") from exc
            raise

    async def _ensure(
        self, operation: str, target: GitHubIssueLabelTarget
    ) -> AdapterExecutionResult:
        intended_present = operation == OP_ENSURE_PRESENT
        before = await self._labels(target, after_mutation=False)
        before_present = label_present(before, target.label)
        changed = False
        if intended_present and not before_present:
            await self._mutate(OP_ENSURE_PRESENT, target)
            changed = True
        elif not intended_present and before_present:
            await self._mutate(OP_ENSURE_ABSENT, target)
            changed = True
        after = await self._labels(target, after_mutation=changed)
        after_present = label_present(after, target.label)
        if after_present != intended_present:
            raise SideEffectExecutionError("external_verification_uncertain")
        return AdapterExecutionResult(
            success=True,
            external_reference=target.external_reference(),
            reversible=True,
            rollback_reference=_encode_rollback(
                prior_present=before_present, changed=changed
            ),
            metadata={
                "state_before_present": before_present,
                "state_after_present": after_present,
                "changed_by_execution": changed,
                "verification_performed": True,
                "verification_status": "confirmed",
                "repository_ref": target.repository(),
                "issue_number": target.issue_number,
                "operation": operation,
                "label_hash": hashlib.sha256(target.label.encode("utf-8")).hexdigest()[:16],
            },
        )

    async def execute(self, action, context) -> AdapterExecutionResult:
        self.mutated = False
        self.calls += 1
        key = getattr(context, "idempotency_key", None) or action.idempotency_key
        if key:
            self.received_idempotency_keys.append(str(key))
        if not self._config.enabled:
            raise SideEffectExecutionError("github_write_adapter_disabled")
        if action.operation not in GITHUB_OPERATIONS:
            raise SideEffectExecutionError("unknown_operation")
        target = self._target(action, context)
        return await self._ensure(action.operation, target)

    async def rollback(self, result, context) -> RollbackResult:
        self.rollback_calls += 1
        if not self._config.enabled:
            raise RollbackExecutionError("github_write_adapter_disabled")
        reference = getattr(result, "rollback_reference", None) or (
            result.get("rollback_reference") if isinstance(result, dict) else None
        )
        if not reference:
            raise RollbackExecutionError("rollback_reference_missing")
        prior_present, _changed = _parse_rollback(str(reference))
        resource = str(getattr(context, "resource", "") or "")
        try:
            target = parse_github_label_resource(resource)
        except GitHubTargetError as exc:
            raise RollbackExecutionError(str(exc.error_code)) from exc
        if not self._config.allows(target.owner, target.repo):
            raise RollbackExecutionError("github_repository_not_allowed")
        inverse = OP_ENSURE_PRESENT if prior_present else OP_ENSURE_ABSENT

        async def _run():
            self.mutated = False
            outcome = await self._ensure(inverse, target)
            if not outcome.success:
                raise RollbackExecutionError("rollback_unconfirmed")
            final_present = bool(outcome.metadata.get("state_after_present"))
            expected = prior_present
            if final_present != expected:
                raise RollbackExecutionError("rollback_unconfirmed")
            return outcome

        try:
            await asyncio.wait_for(_run(), timeout=float(self._config.timeout_seconds))
        except asyncio.TimeoutError as exc:
            raise RollbackExecutionError("external_write_timeout_uncertain") from exc
        except SideEffectExecutionError as exc:
            raise RollbackExecutionError(str(exc.error_code)) from exc
        return RollbackResult(
            success=True,
            rollback_reference=str(reference),
            metadata={"verification_performed": True},
        )

    async def reconcile(self, execution_record, action, context) -> AdapterReconciliationResult:
        self.reconcile_calls += 1
        if action is None:
            return AdapterReconciliationResult(status=ADAPTER_RECON_UNKNOWN)
        try:
            target = parse_github_label_resource(str(action.resource or ""))
        except GitHubTargetError:
            return AdapterReconciliationResult(status=ADAPTER_RECON_UNKNOWN)
        recorded = getattr(execution_record, "external_reference", None)
        computed = target.external_reference()
        if recorded and computed != recorded:
            return AdapterReconciliationResult(
                status=ADAPTER_RECON_UNKNOWN,
                external_reference=computed,
            )
        if not self._config.allows(target.owner, target.repo):
            return AdapterReconciliationResult(status=ADAPTER_RECON_UNKNOWN)
        operation = str(getattr(action, "operation", "") or getattr(execution_record, "operation", ""))
        intended_present = operation == OP_ENSURE_PRESENT
        try:
            names = await self._transport.get_issue_labels(
                target.owner, target.repo, target.issue_number
            )
        except GitHubAdapterError as exc:
            if exc.error_code == "github_resource_not_found_or_inaccessible":
                return AdapterReconciliationResult(
                    status=ADAPTER_RECON_UNKNOWN,
                    external_reference=computed,
                    metadata={"reason": "github_resource_not_found_or_inaccessible"},
                )
            if exc.error_code == "github_timeout_uncertain":
                raise
            return AdapterReconciliationResult(status=ADAPTER_RECON_UNKNOWN)
        present = label_present(names, target.label)
        if present == intended_present:
            return AdapterReconciliationResult(
                status=ADAPTER_RECON_SUCCEEDED,
                external_reference=computed,
                reversible=True,
                rollback_reference=getattr(execution_record, "rollback_reference", None),
                evidence_reference=computed,
                metadata={"state_after_present": present, "verification_status": "confirmed"},
            )
        return AdapterReconciliationResult(
            status=ADAPTER_RECON_FAILED,
            external_reference=computed,
            reversible=True,
            metadata={"state_after_present": present, "verification_status": "confirmed_mismatch"},
        )
