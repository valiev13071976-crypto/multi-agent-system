"""Deterministic artifact manifest builder."""

from __future__ import annotations

from evals.models import ArtifactVersion, canonical_json, content_hash
from evals.registry import VersionRegistry
from evals.versions import (
    CONSISTENCY_VALIDATOR_VERSION,
    CORE_SUITE_VERSION,
    FACT_VALIDATOR_VERSION,
    JUDGE_VERSION,
    POLICY_VERSION,
    PROMPT_VERSION,
    ROLE_REGISTRY_VERSION,
    ROUTING_POLICY_VERSION,
    STRUCTURAL_VALIDATOR_VERSION,
    judge_snapshot,
    memory_policy_snapshot,
    memory_retrieval_policy_snapshot,
    policy_snapshot,
    prompt_content_hash,
    routing_policy_snapshot,
    validator_snapshot,
)


def build_version_registry() -> VersionRegistry:
    reg = VersionRegistry()

    # Prompts / roles
    from agents.role_registry import ROLE_PROMPTS
    from config.config.prompts import (
        BASE_EXPERT_PROMPT,
        CRITIC_PROMPT,
        JUDGE_PROMPT,
        RESEARCHER_PROMPT,
        STRATEGIST_PROMPT,
        TECHNICAL_PROMPT,
        TREND_AGENT_PROMPT,
    )

    prompt_map = {
        "base_expert": BASE_EXPERT_PROMPT,
        "strategist": STRATEGIST_PROMPT,
        "technical": TECHNICAL_PROMPT,
        "researcher": RESEARCHER_PROMPT,
        "critic": CRITIC_PROMPT,
        "trend_agent": TREND_AGENT_PROMPT,
        "judge": JUDGE_PROMPT,
    }
    for pid, text in sorted(prompt_map.items()):
        reg.register(
            ArtifactVersion(
                artifact_type="prompt",
                artifact_id=pid,
                version=PROMPT_VERSION,
                content_hash=prompt_content_hash(text),
            )
        )
    reg.register(
        ArtifactVersion(
            artifact_type="role",
            artifact_id="role_registry",
            version=ROLE_REGISTRY_VERSION,
            content_hash=content_hash(
                {"roles": sorted(ROLE_PROMPTS.keys()), "prompt_version": PROMPT_VERSION}
            ),
        )
    )

    # Router policy
    snap = routing_policy_snapshot()
    reg.register(
        ArtifactVersion(
            artifact_type="router_policy",
            artifact_id="model_router",
            version=ROUTING_POLICY_VERSION,
            content_hash=content_hash(snap),
        )
    )

    # Autonomy policy
    pol = policy_snapshot()
    reg.register(
        ArtifactVersion(
            artifact_type="policy",
            artifact_id="autonomy_gate",
            version=POLICY_VERSION,
            content_hash=content_hash(pol),
        )
    )

    # Tools
    from tools.adapters import github_issue_labels_descriptor, search_tool_descriptor

    for desc in (search_tool_descriptor(), github_issue_labels_descriptor(enabled=False)):
        reg.register(
            ArtifactVersion(
                artifact_type="tool_schema",
                artifact_id=desc.tool_id,
                version=desc.version,
                content_hash=desc.schema_hash
                or content_hash(
                    {
                        "tool_id": desc.tool_id,
                        "operations": list(desc.operations),
                        "trust_level": desc.trust_level,
                    }
                ),
                metadata_safe={"schema_hash": desc.schema_hash},
            )
        )

    # Validators / judge
    for vid, ver in (
        ("structural", STRUCTURAL_VALIDATOR_VERSION),
        ("consistency", CONSISTENCY_VALIDATOR_VERSION),
        ("fact", FACT_VALIDATOR_VERSION),
    ):
        reg.register(
            ArtifactVersion(
                artifact_type="validator",
                artifact_id=vid,
                version=ver,
                content_hash=content_hash(validator_snapshot(vid, ver)),
            )
        )
    reg.register(
        ArtifactVersion(
            artifact_type="judge",
            artifact_id="Judge",
            version=JUDGE_VERSION,
            content_hash=content_hash(judge_snapshot()),
        )
    )

    # Budget guard policy
    from finops.budget_models import BUDGET_POLICY_VERSION
    from finops.budget_policy import budget_policy_snapshot, load_advanced_budget_policies
    from finops.models import BudgetLimits

    budget_policies = load_advanced_budget_policies(
        limits=BudgetLimits(
            per_task=None, per_day=None, per_month=None, unknown_cost_policy="allow"
        )
    )
    # Snapshot includes version constant + policy schema even when empty env limits.
    snap = budget_policy_snapshot(budget_policies)
    snap["budget_policy_version"] = BUDGET_POLICY_VERSION
    reg.register(
        ArtifactVersion(
            artifact_type="policy",
            artifact_id="budget_guard",
            version=BUDGET_POLICY_VERSION,
            content_hash=content_hash(snap),
        )
    )

    # Memory / knowledge policies (P13)
    from memory.models import MEMORY_POLICY_VERSION, MEMORY_RETRIEVAL_VERSION

    mem_pol = memory_policy_snapshot()
    reg.register(
        ArtifactVersion(
            artifact_type="policy",
            artifact_id="memory_policy",
            version=MEMORY_POLICY_VERSION,
            content_hash=content_hash(mem_pol),
        )
    )
    mem_ret = memory_retrieval_policy_snapshot()
    reg.register(
        ArtifactVersion(
            artifact_type="policy",
            artifact_id="memory_retrieval_policy",
            version=MEMORY_RETRIEVAL_VERSION,
            content_hash=content_hash(mem_ret),
        )
    )

    # Suite marker
    reg.register(
        ArtifactVersion(
            artifact_type="eval_suite",
            artifact_id="core",
            version=CORE_SUITE_VERSION,
            content_hash=content_hash(
                {"suite_id": "core", "suite_version": CORE_SUITE_VERSION}
            ),
        )
    )
    return reg


def build_artifact_manifest(registry: VersionRegistry | None = None) -> dict:
    reg = registry or build_version_registry()
    rows = []
    for versions in (
        reg.list_versions(t, i)
        for (t, i) in sorted({(a.artifact_type, a.artifact_id) for a in _all(reg)})
    ):
        for artifact in versions:
            rows.append(
                {
                    "artifact_type": artifact.artifact_type,
                    "artifact_id": artifact.artifact_id,
                    "version": artifact.version,
                    "content_hash": artifact.content_hash,
                    "schema_version": artifact.schema_version,
                }
            )
    rows = sorted(
        rows, key=lambda r: (r["artifact_type"], r["artifact_id"], r["version"])
    )
    payload = {"artifacts": rows, "manifest_version": "1"}
    payload["manifest_hash"] = content_hash(payload["artifacts"])
    return payload


def _all(reg: VersionRegistry):
    # Access private store carefully for enumeration.
    return tuple(reg._items.values())


def assert_version_bumped_if_content_changed(
    baseline: ArtifactVersion, current: ArtifactVersion
) -> None:
    if (
        baseline.artifact_type == current.artifact_type
        and baseline.artifact_id == current.artifact_id
        and baseline.version == current.version
        and baseline.content_hash != current.content_hash
    ):
        raise AssertionError("artifact_changed_without_version_bump")


def manifest_to_json(manifest: dict) -> str:
    return canonical_json(manifest)
