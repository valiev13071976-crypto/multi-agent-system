"""Explicit artifact version constants + content snapshots (no source-file hashing)."""

from __future__ import annotations

from evals.models import content_hash

# Prompt / role (authoritative in agents.role_registry)
from agents.role_registry import PROMPT_VERSION, ROLE_REGISTRY_VERSION  # noqa: E402

# Routing (authoritative constant in agents.model_profile)
from agents.model_profile import ROUTING_POLICY_VERSION  # noqa: E402

# Autonomy (authoritative constant lives in autonomy.policy)
from autonomy.policy import POLICY_VERSION  # noqa: E402

# Validators / judge (authoritative on validators/judge modules)
from agents.fact_validator import VALIDATOR_VERSION as FACT_VALIDATOR_VERSION
from agents.judge import JUDGE_VERSION
from agents.validators.consistency import VALIDATOR_VERSION as CONSISTENCY_VALIDATOR_VERSION
from agents.validators.structural import VALIDATOR_VERSION as STRUCTURAL_VALIDATOR_VERSION

# Eval suite
CORE_SUITE_VERSION = "1.8.0"


def normalize_prompt_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def prompt_content_hash(text: str) -> str:
    return content_hash({"text": normalize_prompt_text(text)})


def policy_snapshot() -> dict:
    """Declarative snapshot of AutonomyGate policy rules (not Python source)."""
    from autonomy.models import (
        ACTION_DELETE,
        ACTION_EXECUTE_CODE,
        ACTION_EXTERNAL_PUBLISH,
        ACTION_FINANCIAL_CHANGE,
        ACTION_PERMISSION_CHANGE,
        ACTION_PURCHASE,
        ACTION_SEND_MESSAGE,
    )
    from autonomy.policy import NEVER_AUTO_ALLOW_TYPES

    return {
        "policy_version": POLICY_VERSION,
        "never_auto_allow": sorted(NEVER_AUTO_ALLOW_TYPES),
        "rules": [
            "deny_by_default",
            "privileged_or_critical_require_approval",
            "irreversible_write_require_approval",
            "bounded_low_internal_allow",
            "bounded_reversible_write_review_after",
            "read_allowed_internal_or_readonly_external",
        ],
        "never_auto_types_check": sorted(
            {
                ACTION_PURCHASE,
                ACTION_FINANCIAL_CHANGE,
                ACTION_SEND_MESSAGE,
                ACTION_EXTERNAL_PUBLISH,
                ACTION_PERMISSION_CHANGE,
                ACTION_DELETE,
                ACTION_EXECUTE_CODE,
            }
        ),
    }


def routing_policy_snapshot() -> dict:
    from agents.model_profile import (
        DEFAULT_AUTO_ROUTING_POLICY,
        POLICY_BALANCED,
        POLICY_COST,
        POLICY_LATENCY,
        POLICY_PRIORITY,
        POLICY_QUALITY,
    )

    return {
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "default_policy": DEFAULT_AUTO_ROUTING_POLICY,
        "policies": sorted(
            {
                POLICY_BALANCED,
                POLICY_COST,
                POLICY_LATENCY,
                POLICY_PRIORITY,
                POLICY_QUALITY,
            }
        ),
    }


def validator_snapshot(validator_id: str, version: str) -> dict:
    return {"validator_id": validator_id, "validator_version": version}


def judge_snapshot() -> dict:
    return {"judge_id": "Judge", "judge_version": JUDGE_VERSION}


def memory_policy_snapshot() -> dict:
    from memory.models import MEMORY_POLICY_VERSION
    from memory.retention import retention_policy_snapshot
    from memory.write_policy import MemoryWritePolicy

    snap = retention_policy_snapshot()
    snap["memory_policy_version"] = MEMORY_POLICY_VERSION
    snap["write_policy_version"] = MemoryWritePolicy.policy_version
    snap["auto_store_default"] = "conservative"
    return snap


def memory_retrieval_policy_snapshot() -> dict:
    from memory.models import MEMORY_RETRIEVAL_VERSION
    from memory.retrieval import retrieval_policy_snapshot

    snap = retrieval_policy_snapshot()
    snap["memory_retrieval_version"] = MEMORY_RETRIEVAL_VERSION
    return snap


def document_policy_snapshot() -> dict:
    from documents.models import DOCUMENT_POLICY_VERSION
    from documents.retention import document_policy_snapshot as snap_fn

    snap = snap_fn()
    snap["document_policy_version"] = DOCUMENT_POLICY_VERSION
    return snap


def document_parser_registry_snapshot() -> dict:
    from documents.models import DOCUMENT_PARSER_REGISTRY_VERSION
    from documents.parsers import parser_registry_snapshot

    snap = parser_registry_snapshot()
    snap["document_parser_registry_version"] = DOCUMENT_PARSER_REGISTRY_VERSION
    return snap


def document_chunker_snapshot() -> dict:
    from documents.chunker import chunker_policy_snapshot
    from documents.models import DOCUMENT_CHUNKER_VERSION

    snap = chunker_policy_snapshot()
    snap["document_chunker_version"] = DOCUMENT_CHUNKER_VERSION
    return snap


def knowledge_policy_snapshot() -> dict:
    from knowledge.models import KNOWLEDGE_POLICY_VERSION
    from knowledge.write_policy import knowledge_policy_snapshot as snap_fn

    snap = snap_fn()
    snap["knowledge_policy_version"] = KNOWLEDGE_POLICY_VERSION
    return snap


def knowledge_retrieval_policy_snapshot() -> dict:
    from knowledge.models import KNOWLEDGE_RETRIEVAL_VERSION
    from knowledge.ranking import retrieval_policy_snapshot

    snap = retrieval_policy_snapshot()
    snap["knowledge_retrieval_version"] = KNOWLEDGE_RETRIEVAL_VERSION
    return snap


def knowledge_source_registry_snapshot() -> dict:
    from knowledge.models import KNOWLEDGE_SOURCE_REGISTRY_VERSION
    from knowledge.registry import source_registry_snapshot

    snap = source_registry_snapshot()
    snap["knowledge_source_registry_version"] = KNOWLEDGE_SOURCE_REGISTRY_VERSION
    return snap


def procurement_policy_snapshot() -> dict:
    from procurement.models import (
        PROCUREMENT_POLICY_VERSION,
        PROCUREMENT_SCORING_VERSION,
        PROCUREMENT_WORKFLOW_VERSION,
    )
    from procurement.policy import procurement_policy_snapshot as snap_fn

    snap = snap_fn()
    snap["procurement_policy_version"] = PROCUREMENT_POLICY_VERSION
    snap["procurement_scoring_version"] = PROCUREMENT_SCORING_VERSION
    snap["procurement_workflow_version"] = PROCUREMENT_WORKFLOW_VERSION
    return snap


def procurement_scoring_snapshot() -> dict:
    from procurement.policy import ProcurementScoringPolicy

    return ProcurementScoringPolicy().as_dict()


def procurement_adapter_schema_snapshot() -> dict:
    from procurement.adapters.descriptors import procurement_adapter_schema_snapshot as snap_fn
    from procurement.adapters.models import PROCUREMENT_ADAPTER_SCHEMA_VERSION

    snap = snap_fn()
    snap["procurement_adapter_schema_version"] = PROCUREMENT_ADAPTER_SCHEMA_VERSION
    return snap


def procurement_external_research_policy_snapshot() -> dict:
    from procurement.adapters.models import PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION
    from procurement.adapters.policy import external_research_policy_snapshot

    snap = external_research_policy_snapshot()
    snap["procurement_external_research_policy_version"] = PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION
    return snap


def procurement_rfq_draft_snapshot() -> dict:
    from procurement.adapters.models import PROCUREMENT_RFQ_DRAFT_VERSION

    return {
        "procurement_rfq_draft_version": PROCUREMENT_RFQ_DRAFT_VERSION,
        "requires_human_send": True,
        "external_send": False,
        "side_effects": 0,
    }


def moonshot_provider_adapter_snapshot() -> dict:
    from agents.moonshot_versions import MOONSHOT_PROVIDER_ADAPTER_VERSION, MOONSHOT_PROVIDER_ID

    return {
        "moonshot_provider_adapter_version": MOONSHOT_PROVIDER_ADAPTER_VERSION,
        "provider_id": MOONSHOT_PROVIDER_ID,
        "enabled_default": False,
        "openai_compatible": True,
        "secret_via_secret_store": True,
        "no_tool_gateway_from_adapter": True,
        "no_failover_redesign": True,
        "pricing_default": "unknown",
    }


def moonshot_model_registry_snapshot() -> dict:
    from agents.moonshot_registry import moonshot_model_registry_snapshot as snap_fn

    return snap_fn()


def procurement_model_eval_snapshot() -> dict:
    from agents.procurement_model_eval import procurement_model_eval_snapshot as snap_fn

    return snap_fn()
