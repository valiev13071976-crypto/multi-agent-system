"""Offline deterministic eval case handlers.

Each handler returns:
  {passed: bool, reason_codes: list, actual: dict, artifact_versions: dict}
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, Callable

from evals.scoring import binary_score
from evals.versions import (
    JUDGE_VERSION,
    POLICY_VERSION,
    PROMPT_VERSION,
    ROUTING_POLICY_VERSION,
)


HandlerResult = dict[str, Any]
HANDLER_REGISTRY: dict[str, Callable[..., HandlerResult]] = {}


def handler(name: str):
    def deco(fn):
        HANDLER_REGISTRY[name] = fn
        return fn

    return deco


def _ok(actual=None, **versions) -> HandlerResult:
    return {
        "passed": True,
        "reason_codes": (),
        "actual": actual or {},
        "artifact_versions": versions,
        "score": 1.0,
    }


def _fail(codes, actual=None, **versions) -> HandlerResult:
    return {
        "passed": False,
        "reason_codes": tuple(codes),
        "actual": actual or {},
        "artifact_versions": versions,
        "score": 0.0,
    }


def _run_async(coro):
    return asyncio.run(coro)


@handler("safety_missing_capability")
def safety_missing_capability(case) -> HandlerResult:
    from autonomy.gate import AutonomyGate, build_proposed_action
    from tools.models import TOOL_TRUST_INTERNAL_SAFE

    gate = AutonomyGate()
    action = build_proposed_action(
        action_type="write",
        workflow_id="wf-eval",
        task_id="t",
        tool_id="test.write",
        operation="set_value",
        resource="test/k",
        tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
        risk_class="low",
        requested_capabilities=("external.write",),
        idempotency_key="idem-miss-cap",
        metadata={"reversible": True},
    )
    decision = gate.evaluate(action, autonomy_level="executor_bounded")
    if decision.decision == "deny" and decision.reason_code == "capability_missing":
        return _ok(
            {"decision": decision.decision, "reason_code": decision.reason_code},
            policy_version=POLICY_VERSION,
        )
    return _fail(
        ["expected_capability_missing_deny"],
        {"decision": decision.decision, "reason_code": decision.reason_code},
        policy_version=POLICY_VERSION,
    )


@handler("safety_disabled_tool")
def safety_disabled_tool(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_WRITE
    from side_effects.github.models import GITHUB_TOOL_ID
    from tools.adapters import github_issue_labels_descriptor
    from tools.gateway import ToolGateway
    from tools.models import ToolRequest
    from tools.registry import ToolRegistry

    async def _run():
        registry = ToolRegistry()
        registry.register(github_issue_labels_descriptor(enabled=False))
        gateway = ToolGateway(registry=registry, register_search=False)
        return await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id=GITHUB_TOOL_ID,
                operation="ensure_label_present",
                arguments={"resource": "github://o/r#1", "label": "x"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="k-disabled",
            )
        )

    result = _run_async(_run())
    if result.error_code == "tool_disabled":
        return _ok({"error_code": result.error_code})
    return _fail(["expected_tool_disabled"], {"error_code": result.error_code})


@handler("safety_unknown_write_op")
def safety_unknown_write_op(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_WRITE
    from side_effects.models import TEST_TOOL_ID, default_test_descriptor
    from side_effects.test_adapter import InMemoryReversibleWriteAdapter
    from tools.adapters import descriptor_from_side_effect
    from tools.gateway import ToolGateway
    from tools.models import TOOL_TRUST_INTERNAL_SAFE, ToolRequest
    from tools.registry import ToolRegistry
    from tests.side_effect_fixtures import caps

    async def _run():
        adapter = InMemoryReversibleWriteAdapter(trust_level=TOOL_TRUST_INTERNAL_SAFE)
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_INTERNAL_SAFE),
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        gateway = ToolGateway(
            registry=registry, register_search=False, side_effect_executor=object()
        )
        return await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id="wf",
                task_id="t",
                tool_id=TEST_TOOL_ID,
                operation="totally_unknown_op",
                arguments={"resource": "test/k", "value": "v"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="k-unknown",
            ),
            capabilities=caps(CAP_EXTERNAL_WRITE),
        )

    result = _run_async(_run())
    if result.error_code in {
        "tool_operation_denied",
        "tool_operation_unknown",
        "tool_policy_denied",
        "unknown_operation",
    } or not result.success:
        return _ok({"error_code": result.error_code, "success": result.success})
    return _fail(["expected_unknown_op_denied"], {"error_code": result.error_code})


@handler("safety_write_bypass_impossible")
def safety_write_bypass_impossible(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
    from autonomy.models import utc_now
    from tools.gateway import ToolGateway
    from tools.models import FORBIDDEN_BYPASS_KEYS, ToolRequest
    from tools.search.fake_provider import FakeSearchProvider

    async def _run():
        gateway = ToolGateway(FakeSearchProvider())
        caps = CapabilitySet(
            subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now()
        )
        denied = []
        for key in sorted(FORBIDDEN_BYPASS_KEYS):
            result = await gateway.invoke(
                ToolRequest(
                    request_id=str(uuid.uuid4()),
                    workflow_id="wf",
                    task_id="t",
                    tool_id="search",
                    operation="search",
                    arguments={"query": "x", key: True},
                    requested_capabilities=(CAP_EXTERNAL_READ,),
                ),
                capabilities=caps,
            )
            if result.error_code != "tool_policy_denied":
                denied.append((key, result.error_code))
        return denied

    bad = _run_async(_run())
    if not bad:
        return _ok({"bypass_keys_checked": len(FORBIDDEN_BYPASS_KEYS)})
    return _fail(["bypass_key_accepted"], {"bad": bad})


@handler("safety_irreversible_default_denied")
def safety_irreversible_default_denied(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_WRITE, CapabilitySet
    from autonomy.gate import AutonomyGate, build_proposed_action
    from autonomy.models import utc_now
    from tools.models import TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE

    gate = AutonomyGate()
    action = build_proposed_action(
        action_type="write",
        workflow_id="wf",
        task_id="t",
        tool_id="ext.write",
        operation="publish",
        resource="ext/r",
        tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
        risk_class="high",
        requested_capabilities=(CAP_EXTERNAL_WRITE,),
        idempotency_key="idem-irr",
        metadata={"reversible": False},
    )
    caps = CapabilitySet(
        subject_id="a", capabilities=(CAP_EXTERNAL_WRITE,), issued_at=utc_now()
    )
    decision = gate.evaluate(
        action, capabilities=caps, autonomy_level="executor_bounded"
    )
    if decision.decision in {"require_approval", "deny"}:
        return _ok(
            {"decision": decision.decision, "reason_code": decision.reason_code},
            policy_version=decision.metadata.get("policy_version", POLICY_VERSION),
        )
    return _fail(
        ["expected_not_auto_allow"],
        {"decision": decision.decision},
        policy_version=POLICY_VERSION,
    )


@handler("safety_missing_idempotency")
def safety_missing_idempotency(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_WRITE, CapabilitySet
    from autonomy.gate import AutonomyGate, build_proposed_action
    from autonomy.models import utc_now
    from tools.models import TOOL_TRUST_INTERNAL_SAFE

    gate = AutonomyGate()
    action = build_proposed_action(
        action_type="write",
        workflow_id="wf",
        task_id="t",
        tool_id="test.write",
        operation="set_value",
        resource="test/k",
        tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
        risk_class="low",
        requested_capabilities=(CAP_EXTERNAL_WRITE,),
        idempotency_key=None,
        metadata={"reversible": True},
    )
    caps = CapabilitySet(
        subject_id="a", capabilities=(CAP_EXTERNAL_WRITE,), issued_at=utc_now()
    )
    decision = gate.evaluate(
        action, capabilities=caps, autonomy_level="executor_bounded"
    )
    if decision.decision == "deny":
        return _ok(
            {"decision": decision.decision, "reason_code": decision.reason_code},
            policy_version=POLICY_VERSION,
        )
    return _fail(
        ["expected_idempotency_deny"],
        {"decision": decision.decision, "reason_code": decision.reason_code},
    )


@handler("safety_uncertain_not_replayed")
def safety_uncertain_not_replayed(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_WRITE, CapabilitySet
    from autonomy.gate import AutonomyGate, build_proposed_action
    from autonomy.idempotency import IdempotencyRegistry
    from autonomy.models import IDEMPOTENCY_UNCERTAIN, utc_now
    from tools.models import TOOL_TRUST_INTERNAL_SAFE

    registry = IdempotencyRegistry()
    registry.reserve("idem-unc", "action-1")
    registry.mark_started("idem-unc")
    registry.mark_uncertain("idem-unc")
    gate = AutonomyGate(idempotency=registry)
    action = build_proposed_action(
        action_id="action-2",
        action_type="write",
        workflow_id="wf",
        task_id="t",
        tool_id="test.write",
        operation="set_value",
        resource="test/k",
        tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
        risk_class="low",
        requested_capabilities=(CAP_EXTERNAL_WRITE,),
        idempotency_key="idem-unc",
        metadata={"reversible": True},
    )
    caps = CapabilitySet(
        subject_id="a", capabilities=(CAP_EXTERNAL_WRITE,), issued_at=utc_now()
    )
    decision = gate.evaluate(
        action, capabilities=caps, autonomy_level="executor_bounded"
    )
    row = registry.get("idem-unc")
    if decision.decision == "deny" and row and row.state == IDEMPOTENCY_UNCERTAIN:
        return _ok(
            {"decision": decision.decision, "reason_code": decision.reason_code},
            policy_version=POLICY_VERSION,
        )
    return _fail(
        ["uncertain_replayed"],
        {"decision": decision.decision, "reason_code": decision.reason_code},
    )


@handler("safety_consumed_permit")
def safety_consumed_permit(case) -> HandlerResult:
    from hitl.errors import ExecutionPermitConsumedError
    from tests.test_execution_permit import ExecutionPermitTests
    from tests.test_hitl_service import T0

    helper = ExecutionPermitTests()
    engine, action, permit = helper._permit()
    engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
    try:
        engine._hitl().consume_for_execution(permit.permit_id, action=action, now=T0)
        return _fail(["consumed_permit_reused"])
    except ExecutionPermitConsumedError:
        return _ok({"status": "consumed_denied"})


@handler("safety_rejected_approval")
def safety_rejected_approval(case) -> HandlerResult:
    from autonomy.models import DECISION_DENY
    from tests.side_effect_fixtures import eval_kwargs, hitl_runtime, se_action
    from tests.test_hitl_service import T0
    from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE

    engine, workflow_id, *_ = hitl_runtime()
    action = se_action(
        workflow_id,
        tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        metadata={"reversible": True},
        risk_class="medium",
    )
    kwargs = eval_kwargs(level="executor_confirmed")
    engine.evaluate_action(
        action,
        requested_by="agent-1",
        **kwargs,
    )
    approval_id = engine.last_approval_id
    engine._hitl().reject(approval_id, resolved_by="reviewer-1", now=T0)
    approval = engine._hitl().get(approval_id)
    decision = engine._gate().evaluate(
        action,
        approval=approval,
        **kwargs,
    )
    if decision.decision == DECISION_DENY:
        return _ok(
            {"decision": decision.decision, "reason_code": decision.reason_code},
            policy_version=POLICY_VERSION,
        )
    return _fail(
        ["rejected_approval_allowed"],
        {"decision": decision.decision, "reason_code": decision.reason_code},
    )


@handler("safety_expired_permit")
def safety_expired_permit(case) -> HandlerResult:
    from datetime import timedelta

    from hitl.errors import ExecutionPermitExpiredError
    from tests.test_execution_permit import ExecutionPermitTests
    from tests.test_hitl_service import T0

    helper = ExecutionPermitTests()
    engine, action, permit = helper._permit()
    try:
        engine._hitl().permits.validate(
            permit, action=action, now=T0 + timedelta(seconds=301)
        )
        return _fail(["expired_permit_accepted"])
    except ExecutionPermitExpiredError:
        return _ok({"status": "expired_denied"})


@handler("safety_dry_run_zero_mutation")
def safety_dry_run_zero_mutation(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_WRITE
    from side_effects.models import TEST_TOOL_ID, default_test_descriptor
    from side_effects.registry import SideEffectAdapterRegistry
    from side_effects.test_adapter import InMemoryReversibleWriteAdapter
    from tests.side_effect_fixtures import T0, caps, eval_kwargs
    from tools.adapters import descriptor_from_side_effect
    from tools.gateway import ToolGateway
    from tools.models import TOOL_TRUST_INTERNAL_SAFE, ToolRequest
    from tools.registry import ToolRegistry
    from workflow.engine import WorkflowEngine

    async def _run():
        engine = WorkflowEngine()
        workflow_id = engine.create("t")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        adapter = InMemoryReversibleWriteAdapter(trust_level=TOOL_TRUST_INTERNAL_SAFE)
        gate = engine._gate()

        class DryExecutor:
            mutate_calls = 0

            async def dry_run(self, action, **kwargs):
                class Planned:
                    would_execute = True
                    would_change = True
                    would_require_approval = False

                return Planned()

            async def execute(self, *a, **k):
                self.mutate_calls += 1
                raise AssertionError("execute must not run on dry_run")

        dry_exec = DryExecutor()
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_INTERNAL_SAFE),
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        gateway = ToolGateway(
            registry=registry,
            side_effect_executor=dry_exec,
            gate=gate,
            register_search=False,
        )
        result = await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                task_id="t",
                tool_id=TEST_TOOL_ID,
                operation="set_value",
                arguments={"resource": "test/key", "value": "v"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="dry-idem-eval",
                dry_run=True,
            ),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=gate,
            executor=dry_exec,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )
        return result, adapter.calls, dry_exec.mutate_calls

    result, adapter_calls, mutate_calls = _run_async(_run())
    if result.success and adapter_calls == 0 and mutate_calls == 0:
        return _ok(
            {
                "dry_run": True,
                "adapter_calls": adapter_calls,
                "mutate_calls": mutate_calls,
            }
        )
    return _fail(
        ["dry_run_mutated"],
        {
            "success": result.success,
            "error_code": result.error_code,
            "adapter_calls": adapter_calls,
            "mutate_calls": mutate_calls,
        },
    )


@handler("safety_secret_metadata")
def safety_secret_metadata(case) -> HandlerResult:
    from autonomy.models import sanitize_metadata

    dirty = {
        "note": "ok",
        "GITHUB_WRITE_TOKEN": "ghp_secret",
        "PANDA_ENCRYPTION_KEY": "enc",
        "authorization": "Bearer xyz",
        "api_key": "sk-test",
    }
    cleaned = sanitize_metadata(dirty)
    blob = str(cleaned).lower()
    for needle in ("ghp_secret", "enc", "bearer xyz", "sk-test", "github_write_token"):
        if needle.lower() in blob and needle != "ok":
            # keys stripped; values must not appear
            if needle in ("ghp_secret", "enc", "bearer xyz", "sk-test"):
                return _fail(["secret_leaked"], {"cleaned": cleaned})
    if "note" not in cleaned:
        return _fail(["safe_key_removed"], {"cleaned": cleaned})
    return _ok({"keys": sorted(cleaned.keys())})


@handler("safety_dynamic_execution_denied")
def safety_dynamic_execution_denied(case) -> HandlerResult:
    from tools.gateway import ToolGateway
    from tools.models import FORBIDDEN_DYNAMIC_KEYS, ToolRequest
    from tools.search.fake_provider import FakeSearchProvider

    async def _run():
        gateway = ToolGateway(FakeSearchProvider())
        bad = []
        for key in ("module_path", "python_code", "shell_command", "base_url"):
            if key not in FORBIDDEN_DYNAMIC_KEYS:
                bad.append(("missing_forbidden", key))
                continue
            result = await gateway.invoke(
                ToolRequest(
                    request_id=str(uuid.uuid4()),
                    workflow_id="wf",
                    task_id="t",
                    tool_id="search",
                    operation="search",
                    arguments={"query": "x", key: "evil"},
                )
            )
            if result.error_code != "tool_argument_invalid":
                bad.append((key, result.error_code))
        return bad

    bad = _run_async(_run())
    if not bad:
        return _ok({"checked": True})
    return _fail(["dynamic_execution_allowed"], {"bad": bad})


@handler("safety_github_write_disabled_default")
def safety_github_write_disabled_default(case) -> HandlerResult:
    from tools.adapters import github_issue_labels_descriptor

    desc = github_issue_labels_descriptor(enabled=False)
    if not desc.enabled:
        return _ok(
            {
                "tool_id": desc.tool_id,
                "version": desc.version,
                "schema_hash": desc.schema_hash,
                "enabled": False,
            }
        )
    return _fail(["github_write_enabled_by_default"], {"enabled": desc.enabled})


@handler("compat_analyze_public_keys")
def compat_analyze_public_keys(case) -> HandlerResult:
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    from tests.test_smoke import CONTRACT_KEYS, load_app

    main_mod = load_app(
        OPENAI_API_KEY="fake-key",
        OPENAI_MODEL="fake-model",
    )
    with patch.object(
        main_mod.router.pipeline.expert_manager.openai,
        "run",
        new=AsyncMock(return_value="successful strategist answer"),
    ):
        client = TestClient(main_mod.app)
        response = client.post(
            "/api/analyze",
            json={"prompt": "eval compat check", "mode": "openai"},
        )
    if response.status_code != 200:
        return _fail(
            ["analyze_http_error"],
            {"status_code": response.status_code},
        )
    payload = response.json()
    keys = set(payload.keys())
    expected = set(CONTRACT_KEYS)
    if keys != expected:
        return _fail(
            ["public_contract_changed"],
            {"keys": sorted(keys), "expected": sorted(expected)},
        )
    if payload.get("role") != "Judge":
        return _fail(["role_not_judge"], {"role": payload.get("role")})
    return _ok({"keys": sorted(keys), "role": payload.get("role")})


@handler("routing_offline_basic")
def routing_offline_basic(case) -> HandlerResult:
    from agents.model_profile import ROLE_TO_ROUTING_CATEGORY
    from agents.model_router import ModelRouter
    from agents.role_registry import role_version_metadata
    from agents.task_classifier import classify_task
    from tests.test_model_router import registry_with

    classification = classify_task("Сравни архитектуры микросервисов и монолита")
    role = "technical"
    category = ROLE_TO_ROUTING_CATEGORY[role]
    router = ModelRouter(registry_with("openai"))
    decision = router.decide("auto", role, category=category)
    meta = role_version_metadata(role)
    if (
        decision.routing_policy_version == ROUTING_POLICY_VERSION
        and meta["prompt_version"] == PROMPT_VERSION
        and classification.category
        and "openai" in decision.provider_ids
    ):
        return _ok(
            {
                "category": classification.category,
                "providers": list(decision.provider_ids),
                "reason": decision.reason,
                "role_meta": meta,
            },
            routing_policy_version=ROUTING_POLICY_VERSION,
            prompt_version=PROMPT_VERSION,
        )
    return _fail(
        ["routing_unexpected"],
        {
            "decision_version": decision.routing_policy_version,
            "category": getattr(classification, "category", None),
            "providers": list(decision.provider_ids),
        },
    )


@handler("validator_structural_offline")
def validator_structural_offline(case) -> HandlerResult:
    from agents.validators.structural import VALIDATOR_VERSION, StructuralValidator

    v = StructuralValidator()
    bad = v.validate("")
    good = v.validate("A complete expert answer with substance.")
    if bad.status == "fail" and good.status == "pass":
        return _ok(
            {
                "bad": bad.status,
                "good": good.status,
                "validator_version": VALIDATOR_VERSION,
            },
            validator_version=VALIDATOR_VERSION,
        )
    return _fail(
        ["validator_unexpected"],
        {"bad": bad.status, "good": good.status},
        validator_version=VALIDATOR_VERSION,
    )


@handler("judge_offline_shape")
def judge_offline_shape(case) -> HandlerResult:
    from agents.judge import JUDGE_VERSION, Judge
    from tests.test_smoke import CONTRACT_KEYS

    judge = Judge()
    result = judge._legacy_run("stable offline judge prompt")
    if set(result.keys()) != set(CONTRACT_KEYS):
        return _fail(
            ["judge_shape_changed"],
            {"keys": sorted(result.keys())},
            judge_version=JUDGE_VERSION,
        )
    if result.get("role") != "Judge":
        return _fail(["judge_role_changed"], {"role": result.get("role")})
    return _ok(
        {"keys": sorted(result.keys())},
        judge_version=judge.judge_version,
    )


@handler("workflow_lifecycle")
def workflow_lifecycle(case) -> HandlerResult:
    from workflow.engine import WorkflowEngine
    from workflow.models import (
        STATUS_CANCELLED,
        STATUS_COMPLETED,
        STATUS_CREATED,
        STATUS_FAILED,
        STATUS_PLANNED,
        STATUS_RUNNING,
        STATUS_VALIDATING,
        STATUS_WAITING_APPROVAL,
    )

    engine = WorkflowEngine()
    wf = engine.create("eval-lifecycle")
    sm = engine.state_manager
    assert sm.get(wf).status == STATUS_CREATED
    sm.plan(wf)
    assert sm.get(wf).status == STATUS_PLANNED
    sm.start(wf)
    assert sm.get(wf).status == STATUS_RUNNING
    sm.wait_for_approval(wf)
    assert sm.get(wf).status == STATUS_WAITING_APPROVAL
    sm.approve(wf)
    assert sm.get(wf).status == STATUS_RUNNING
    sm.mark_validating(wf)
    assert sm.get(wf).status == STATUS_VALIDATING
    sm.complete_workflow(wf)
    assert sm.get(wf).status == STATUS_COMPLETED
    try:
        sm.start(wf)
        return _fail(["terminal_reopened"])
    except Exception:
        pass

    wf2 = engine.create("eval-fail")
    sm.plan(wf2)
    sm.start(wf2)
    sm.fail_workflow(wf2, "eval")
    assert sm.get(wf2).status == STATUS_FAILED

    wf3 = engine.create("eval-cancel")
    sm.plan(wf3)
    sm.cancel(wf3)
    assert sm.get(wf3).status == STATUS_CANCELLED
    return _ok({"lifecycle": "ok"})


@handler("autonomy_allow_require_deny")
def autonomy_allow_require_deny(case) -> HandlerResult:
    from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_EXTERNAL_WRITE, CapabilitySet
    from autonomy.gate import AutonomyGate, build_proposed_action
    from autonomy.models import utc_now
    from tools.models import (
        TOOL_TRUST_INTERNAL_SAFE,
        TOOL_TRUST_READ_ONLY_EXTERNAL,
        TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    )

    gate = AutonomyGate()
    now = utc_now()
    read_caps = CapabilitySet(
        subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=now
    )
    write_caps = CapabilitySet(
        subject_id="a", capabilities=(CAP_EXTERNAL_WRITE,), issued_at=now
    )
    read = gate.evaluate(
        build_proposed_action(
            action_type="read",
            workflow_id="wf",
            task_id="t",
            tool_id="search",
            operation="search",
            resource="web",
            tool_trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
            risk_class="low",
            requested_capabilities=(CAP_EXTERNAL_READ,),
        ),
        capabilities=read_caps,
        autonomy_level="analyst",
        now=now,
    )
    require = gate.evaluate(
        build_proposed_action(
            action_type="write",
            workflow_id="wf",
            task_id="t",
            tool_id="ext",
            operation="publish",
            resource="ext/r",
            tool_trust_level=TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
            risk_class="high",
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            idempotency_key="idem-req",
            metadata={"reversible": False},
        ),
        capabilities=write_caps,
        autonomy_level="executor_bounded",
        now=now,
    )
    deny = gate.evaluate(
        build_proposed_action(
            action_type="write",
            workflow_id="wf",
            task_id="t",
            tool_id="test",
            operation="set_value",
            resource="t/k",
            tool_trust_level=TOOL_TRUST_INTERNAL_SAFE,
            risk_class="low",
            requested_capabilities=(CAP_EXTERNAL_WRITE,),
            idempotency_key="idem-deny",
            metadata={"reversible": True},
        ),
        autonomy_level="executor_bounded",
        now=now,
    )
    actual = {
        "read": read.decision,
        "require": require.decision,
        "deny": deny.decision,
        "policy_version": read.metadata.get("policy_version"),
    }
    if (
        read.decision == "allow"
        and require.decision == "require_approval"
        and deny.decision == "deny"
    ):
        return _ok(actual, policy_version=POLICY_VERSION)
    return _fail(["autonomy_matrix_unexpected"], actual, policy_version=POLICY_VERSION)


@handler("version_bump_invariant_demo")
def version_bump_invariant_demo(case) -> HandlerResult:
    """Meta-check used by unit tests; core suite uses real manifest compare."""
    from evals.manifest import assert_version_bumped_if_content_changed
    from evals.models import ArtifactVersion

    a = ArtifactVersion("policy", "autonomy_gate", "1.0.0", "aaa")
    b = ArtifactVersion("policy", "autonomy_gate", "1.0.0", "bbb")
    try:
        assert_version_bumped_if_content_changed(a, b)
        return _fail(["expected_version_bump_assertion"])
    except AssertionError as exc:
        if "artifact_changed_without_version_bump" in str(exc):
            c = ArtifactVersion("policy", "autonomy_gate", "1.0.1", "bbb")
            assert_version_bumped_if_content_changed(a, c)
            return _ok({"invariant": "enforced"})
        return _fail(["unexpected_assertion"], {"error": str(exc)})


@handler("finops_hard_limit_terminates")
def finops_hard_limit_terminates(case) -> HandlerResult:
    from decimal import Decimal

    from finops.budget_guard import BudgetGuard
    from finops.budget_models import DECISION_TERMINATE, BudgetPolicy, SCOPE_GLOBAL
    from finops.models import BudgetLimits, PriceQuote
    from finops.service import FinOpsService

    quote = PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)
    finops = FinOpsService(
        prices={("openai", "m"): quote},
        limits=BudgetLimits(None, None, None, "allow"),
    )
    guard = BudgetGuard(
        finops=finops,
        policies=(
            BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("5")),
        ),
        required=True,
    )
    d = guard.evaluate(
        task_id="t",
        provider="openai",
        model="m",
        estimated_cost=Decimal("6"),
    )
    if d.decision == DECISION_TERMINATE:
        return _ok({"decision": d.decision, "reason": d.reason_code})
    return _fail(["expected_terminate"], {"decision": d.decision})


@handler("finops_missing_reservation_blocks")
def finops_missing_reservation_blocks(case) -> HandlerResult:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
    from agents.provider_result import ProviderResult
    from finops.budget_guard import BudgetGuard
    from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
    from finops.budget_store import BudgetPersistenceUnavailableError, InMemoryBudgetStore
    from finops.models import BudgetLimits, PriceQuote
    from finops.service import FinOpsService

    class BoomStore(InMemoryBudgetStore):
        def begin_reserve_transaction(self):
            raise BudgetPersistenceUnavailableError()

    quote = PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)
    finops = FinOpsService(prices={("openai", "m"): quote}, limits=BudgetLimits(None, None, None, "allow"))
    guard = BudgetGuard(
        finops=finops,
        policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("100")),),
        store=BoomStore(),
        required=True,
    )

    class Agent:
        model = "m"

        async def run(self, prompt):
            return ProviderResult("x", "openai", "m", 1000, 500, 1500)

    manager = ExpertManager(openai=Agent(), finops=finops, budget_guard=guard)
    try:
        _run_async(manager.run("p"))
        return _fail(["expected_block"])
    except FinOpsBudgetDeniedError as exc:
        if manager.provider_calls == 0 and "budget_persistence" in exc.reason:
            return _ok({"reason": exc.reason, "provider_calls": 0})
        return _fail(["unexpected_reason"], {"reason": exc.reason, "calls": manager.provider_calls})


@handler("finops_concurrent_no_overspend")
def finops_concurrent_no_overspend(case) -> HandlerResult:
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal

    from finops.budget_guard import BudgetGuard, BudgetGuardError
    from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
    from finops.models import BudgetLimits, PriceQuote
    from finops.service import FinOpsService

    quote = PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)
    finops = FinOpsService(prices={("openai", "m"): quote}, limits=BudgetLimits(None, None, None, "allow"))
    guard = BudgetGuard(
        finops=finops,
        policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("10")),),
        required=True,
    )

    def try_reserve(i):
        try:
            return guard.reserve(
                task_id=f"t{i}",
                provider="openai",
                model="m",
                estimated_cost=Decimal("7"),
            )
        except BudgetGuardError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(try_reserve, range(2)))
    ok = [r for r in results if r is not None]
    reserved = guard.ledger.get_reserved("global")
    if len(ok) == 1 and reserved <= Decimal("10"):
        return _ok({"reserved": str(reserved), "wins": len(ok)})
    return _fail(["overspend"], {"reserved": str(reserved), "wins": len(ok)})


@handler("finops_release_restores_capacity")
def finops_release_restores_capacity(case) -> HandlerResult:
    from decimal import Decimal

    from finops.budget_guard import BudgetGuard
    from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
    from finops.models import BudgetLimits, PriceQuote
    from finops.service import FinOpsService

    quote = PriceQuote("openai", "m", Decimal("1"), Decimal("1"), "USD", True)
    finops = FinOpsService(prices={("openai", "m"): quote}, limits=BudgetLimits(None, None, None, "allow"))
    guard = BudgetGuard(
        finops=finops,
        policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("10")),),
        required=True,
    )
    r = guard.reserve(task_id="t", provider="openai", model="m", estimated_cost=Decimal("8"))
    guard.release(r.reservation_id)
    remaining = guard.get_remaining_budget("global")
    if remaining == Decimal("10"):
        return _ok({"remaining": str(remaining)})
    return _fail(["capacity_not_restored"], {"remaining": str(remaining)})


@handler("finops_degrade_capability_safe")
def finops_degrade_capability_safe(case) -> HandlerResult:
    from decimal import Decimal

    from finops.budget_guard import BudgetGuard
    from finops.budget_models import DECISION_TERMINATE, BudgetPolicy, SCOPE_GLOBAL
    from finops.models import BudgetLimits, PriceQuote
    from finops.service import FinOpsService

    prices = {
        ("openai", "expensive"): PriceQuote("openai", "expensive", Decimal("10"), Decimal("10"), "USD", True),
        ("anthropic", "cheap"): PriceQuote("anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True),
    }
    finops = FinOpsService(prices=prices, limits=BudgetLimits(None, None, None, "allow"))
    guard = BudgetGuard(
        finops=finops,
        policies=(
            BudgetPolicy(
                "g",
                SCOPE_GLOBAL,
                hard_limit=Decimal("20"),
                soft_limit=Decimal("15"),
                degrade_threshold=Decimal("15"),
            ),
        ),
        required=True,
    )
    # Spend down so remaining is soft
    guard.store.add_spent("global:", Decimal("10"))
    d = guard.evaluate(
        task_id="t",
        provider="openai",
        model="expensive",
        estimated_cost=Decimal("6"),
        capable_candidates=(),  # no capable cheaper → TERMINATE
    )
    if d.decision == DECISION_TERMINATE:
        return _ok({"decision": d.decision})
    return _fail(["expected_terminate_no_capable"], {"decision": d.decision})


@handler("finops_unknown_cost_not_zero")
def finops_unknown_cost_not_zero(case) -> HandlerResult:
    from finops.budget_guard import BudgetGuard
    from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL, DECISION_TERMINATE
    from finops.models import BudgetLimits
    from finops.service import FinOpsService
    from decimal import Decimal

    finops = FinOpsService(
        prices={},
        limits=BudgetLimits(None, None, None, "deny"),
    )
    guard = BudgetGuard(
        finops=finops,
        policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("10")),),
        required=True,
    )
    d = guard.evaluate(
        task_id="t", provider="openai", model="m", estimated_cost=None
    )
    if d.decision == DECISION_TERMINATE and d.requested_cost is None:
        return _ok({"decision": d.decision, "reason": d.reason_code})
    return _fail(["unknown_became_free"], {"decision": d.decision})


def get_handler(name: str):
    if name not in HANDLER_REGISTRY:
        raise KeyError(f"unknown_handler:{name}")
    return HANDLER_REGISTRY[name]
