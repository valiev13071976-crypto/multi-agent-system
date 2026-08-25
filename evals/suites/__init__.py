"""Core offline eval suite definitions."""

from __future__ import annotations

from evals.models import EvalCase, EvalSuite
from evals.versions import CORE_SUITE_VERSION

CASE_VERSION = "1.0.0"


def _case(
    case_id: str,
    category: str,
    handler: str,
    *,
    description: str,
    critical: bool = False,
    requires_network: bool = False,
    tags: tuple[str, ...] = (),
    expected: dict | None = None,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        suite_id="core",
        case_version=CASE_VERSION,
        category=category,
        description=description,
        handler=handler,
        critical=critical,
        deterministic=True,
        requires_network=requires_network,
        tags=tags,
        expected=expected or {"passed": True},
    )


def build_core_suite() -> EvalSuite:
    cases = (
        _case(
            "safety_missing_capability",
            "security",
            "safety_missing_capability",
            description="Missing capability denied",
            critical=True,
            tags=("critical", "autonomy"),
        ),
        _case(
            "safety_disabled_tool",
            "tool_gateway_write",
            "safety_disabled_tool",
            description="Disabled tool denied",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_unknown_write_op",
            "tool_gateway_write",
            "safety_unknown_write_op",
            description="Unknown write operation denied",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_write_bypass_impossible",
            "security",
            "safety_write_bypass_impossible",
            description="Write bypass keys denied",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_irreversible_default_denied",
            "autonomy",
            "safety_irreversible_default_denied",
            description="Irreversible/privileged default not auto-allowed",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_missing_idempotency",
            "idempotency",
            "safety_missing_idempotency",
            description="Missing idempotency denied",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_uncertain_not_replayed",
            "idempotency",
            "safety_uncertain_not_replayed",
            description="Uncertain side effect not replayed",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_consumed_permit",
            "permit",
            "safety_consumed_permit",
            description="Consumed permit not reusable",
            critical=True,
            tags=("critical", "hitl"),
        ),
        _case(
            "safety_rejected_approval",
            "hitl",
            "safety_rejected_approval",
            description="Rejected approval cannot execute",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_expired_permit",
            "permit",
            "safety_expired_permit",
            description="Expired permit cannot execute",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_dry_run_zero_mutation",
            "tool_gateway_write",
            "safety_dry_run_zero_mutation",
            description="Dry-run causes zero mutation",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_secret_metadata",
            "security",
            "safety_secret_metadata",
            description="Secret metadata not emitted",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_dynamic_execution_denied",
            "security",
            "safety_dynamic_execution_denied",
            description="Arbitrary URL/module/shell execution denied",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "safety_github_write_disabled_default",
            "security",
            "safety_github_write_disabled_default",
            description="GitHub write disabled by default",
            critical=True,
            tags=("critical",),
        ),
        _case(
            "compat_analyze_public_keys",
            "compatibility",
            "compat_analyze_public_keys",
            description="/api/analyze public success keys exact",
            critical=True,
            tags=("critical", "compatibility"),
        ),
        _case(
            "routing_offline_basic",
            "routing",
            "routing_offline_basic",
            description="Offline classifier/role/router versions",
            critical=False,
            tags=("routing", "roles"),
        ),
        _case(
            "validator_structural_offline",
            "validation",
            "validator_structural_offline",
            description="Structural validator offline",
            critical=False,
            tags=("validation",),
        ),
        _case(
            "judge_offline_shape",
            "judge",
            "judge_offline_shape",
            description="Judge public shape offline",
            critical=False,
            tags=("judge",),
        ),
        _case(
            "workflow_lifecycle",
            "workflow",
            "workflow_lifecycle",
            description="Workflow lifecycle + terminal lock",
            critical=False,
            tags=("workflow",),
        ),
        _case(
            "autonomy_allow_require_deny",
            "autonomy",
            "autonomy_allow_require_deny",
            description="Representative ALLOW/REQUIRE_APPROVAL/DENY",
            critical=False,
            tags=("autonomy",),
        ),
        _case(
            "version_bump_invariant_demo",
            "compatibility",
            "version_bump_invariant_demo",
            description="Version bump invariant helper",
            critical=False,
            tags=("versioning",),
        ),
        _case(
            "network_optional_skipped",
            "observability",
            "routing_offline_basic",
            description="Network-required case skipped when disabled",
            critical=False,
            requires_network=True,
            tags=("network",),
        ),
    )
    return EvalSuite(
        suite_id="core",
        suite_version=CORE_SUITE_VERSION,
        description="P10 deterministic offline core regression suite",
        cases=cases,
        required_pass_rate=1.0,
        critical_case_policy="fail_run",
        metadata={"allow_network_default": False},
    )


SUITES = {
    "core": build_core_suite,
}


def get_suite(suite_id: str) -> EvalSuite:
    if suite_id not in SUITES:
        raise KeyError(f"unknown_suite:{suite_id}")
    return SUITES[suite_id]()
