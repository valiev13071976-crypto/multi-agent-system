"""Canonical eval / version models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def content_hash(value: Any) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _meta(value) -> Mapping[str, object]:
    from autonomy.models import sanitize_metadata

    return MappingProxyType(sanitize_metadata(value or {}))


ARTIFACT_TYPES = (
    "prompt",
    "role",
    "tool_schema",
    "policy",
    "router_policy",
    "validator",
    "judge",
    "workflow_definition",
    "eval_suite",
)

CASE_STATUSES = ("passed", "failed", "skipped", "error")
RUN_STATUSES = ("passed", "failed", "blocked")


@dataclass(frozen=True)
class ArtifactVersion:
    artifact_type: str
    artifact_id: str
    version: str
    content_hash: str
    schema_version: str = "1"
    created_at: datetime | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.artifact_type not in ARTIFACT_TYPES:
            raise ValueError(f"invalid_artifact_type:{self.artifact_type}")
        if not str(self.version or "").strip():
            raise ValueError("version_required")
        if not str(self.content_hash or "").strip():
            raise ValueError("content_hash_required")
        stamp = self.created_at or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", stamp)
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    suite_id: str
    case_version: str
    category: str
    description: str
    input: Mapping[str, object] = field(default_factory=dict)
    expected: Mapping[str, object] = field(default_factory=dict)
    constraints: Mapping[str, object] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    critical: bool = False
    deterministic: bool = True
    requires_network: bool = False
    handler: str = ""
    created_at: datetime | None = None

    def __post_init__(self):
        object.__setattr__(self, "input", _meta(self.input))
        object.__setattr__(self, "expected", _meta(self.expected))
        object.__setattr__(self, "constraints", _meta(self.constraints))
        object.__setattr__(self, "tags", tuple(self.tags))


@dataclass(frozen=True)
class EvalSuite:
    suite_id: str
    suite_version: str
    description: str
    cases: tuple[EvalCase, ...]
    required_pass_rate: float = 1.0
    critical_case_policy: str = "fail_run"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "cases", tuple(self.cases))
        object.__setattr__(self, "metadata", _meta(self.metadata))

    @property
    def content_hash(self) -> str:
        payload = {
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "cases": [
                {
                    "case_id": c.case_id,
                    "case_version": c.case_version,
                    "category": c.category,
                    "critical": c.critical,
                    "handler": c.handler,
                    "expected": dict(c.expected),
                }
                for c in self.cases
            ],
        }
        return content_hash(payload)


@dataclass(frozen=True)
class EvalCaseResult:
    run_id: str
    case_id: str
    status: str
    passed: bool
    score: float
    reason_codes: tuple[str, ...] = ()
    duration_ms: int = 0
    artifact_versions: Mapping[str, object] = field(default_factory=dict)
    actual_summary_safe: Mapping[str, object] = field(default_factory=dict)
    expected_summary_safe: Mapping[str, object] = field(default_factory=dict)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    critical: bool = False

    def __post_init__(self):
        if self.status not in CASE_STATUSES:
            raise ValueError(f"invalid_case_status:{self.status}")
        score = float(self.score)
        if score < 0.0:
            score = 0.0
        if score > 1.0:
            score = 1.0
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "artifact_versions", _meta(self.artifact_versions))
        object.__setattr__(self, "actual_summary_safe", _meta(self.actual_summary_safe))
        object.__setattr__(
            self, "expected_summary_safe", _meta(self.expected_summary_safe)
        )
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class EvalRun:
    run_id: str
    suite_id: str
    suite_version: str
    started_at: datetime
    completed_at: datetime
    total: int
    passed: int
    failed: int
    skipped: int
    pass_rate: float
    critical_failures: tuple[str, ...]
    status: str
    case_results: tuple[EvalCaseResult, ...] = ()
    git_commit: str | None = None
    environment_ref: str = "offline"
    baseline_reference: str | None = None
    regressions: tuple[str, ...] = ()
    improvements: tuple[str, ...] = ()
    artifact_versions: Mapping[str, object] = field(default_factory=dict)
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in RUN_STATUSES:
            raise ValueError(f"invalid_run_status:{self.status}")
        object.__setattr__(self, "critical_failures", tuple(self.critical_failures))
        object.__setattr__(self, "case_results", tuple(self.case_results))
        object.__setattr__(self, "regressions", tuple(self.regressions))
        object.__setattr__(self, "improvements", tuple(self.improvements))
        object.__setattr__(self, "artifact_versions", _meta(self.artifact_versions))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class EvalBaseline:
    baseline_id: str
    suite_id: str
    suite_version: str
    reference_commit: str | None
    summary: Mapping[str, object]
    case_outcomes: Mapping[str, object]
    artifact_versions: Mapping[str, object]
    created_at: datetime
    critical_case_ids: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "summary", _meta(self.summary))
        object.__setattr__(self, "case_outcomes", _meta(self.case_outcomes))
        object.__setattr__(self, "artifact_versions", _meta(self.artifact_versions))
        object.__setattr__(self, "critical_case_ids", tuple(self.critical_case_ids))
