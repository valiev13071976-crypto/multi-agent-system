from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

ALLOWED_STATUSES = (
    STATUS_PASS,
    STATUS_WARN,
    STATUS_FAIL,
    STATUS_UNKNOWN,
)

FACT_HEAVY_CATEGORIES = frozenset({"research", "trend_analysis"})


def _as_score(value) -> float:
    score = float(value)
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


@dataclass(frozen=True)
class ValidationResult:
    validator_id: str
    status: str
    score: float
    issues: tuple[str, ...]
    evidence: Mapping[str, object]
    reason: str

    def __post_init__(self):
        status = str(self.status)
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Invalid validation status: {status!r}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "score", _as_score(self.score))
        object.__setattr__(self, "issues", tuple(str(item) for item in self.issues))
        object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(
            self,
            "evidence",
            MappingProxyType(dict(self.evidence or {})),
        )


@dataclass(frozen=True)
class PeerReviewResult:
    validator_id: str
    status: str
    score: float
    issues: tuple[str, ...]
    evidence: Mapping[str, object]
    reason: str
    answered_provider_ids: tuple[str, ...]
    failed_provider_ids: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "status", str(self.status))
        object.__setattr__(self, "score", _as_score(self.score))
        object.__setattr__(self, "issues", tuple(str(item) for item in self.issues))
        object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(
            self,
            "answered_provider_ids",
            tuple(self.answered_provider_ids),
        )
        object.__setattr__(
            self,
            "failed_provider_ids",
            tuple(self.failed_provider_ids),
        )
        object.__setattr__(
            self,
            "evidence",
            MappingProxyType(dict(self.evidence or {})),
        )


@dataclass(frozen=True)
class ConfidenceInputs:
    successful_experts: int
    failed_providers: int
    structural_fail: bool
    structural_all_pass: bool
    consistency_status: str
    sources_present: bool
    factual_status: str
    category: str | None
