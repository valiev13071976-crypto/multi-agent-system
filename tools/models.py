from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping


TRUST_UNKNOWN = "unknown"
TRUST_LOW = "low"
TRUST_MEDIUM = "medium"
TRUST_HIGH = "high"
ALLOWED_TRUST = (TRUST_UNKNOWN, TRUST_LOW, TRUST_MEDIUM, TRUST_HIGH)

EVIDENCE_SUPPORTED = "supported"
EVIDENCE_CONTRADICTED = "contradicted"
EVIDENCE_INSUFFICIENT = "insufficient"
EVIDENCE_UNKNOWN = "unknown"
ALLOWED_EVIDENCE_STATUS = (
    EVIDENCE_SUPPORTED,
    EVIDENCE_CONTRADICTED,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_UNKNOWN,
)

TOOL_TRUST_INTERNAL_SAFE = "INTERNAL_SAFE"
TOOL_TRUST_READ_ONLY_EXTERNAL = "READ_ONLY_EXTERNAL"
TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE = "WRITE_EXTERNAL_REVERSIBLE"
TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE = "WRITE_EXTERNAL_IRREVERSIBLE"
TOOL_TRUST_PRIVILEGED = "PRIVILEGED"
TOOL_TRUST_LEVELS = (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    TOOL_TRUST_PRIVILEGED,
)

MAX_FACT_CLAIMS = 3
MAX_SEARCH_RESULTS_PER_CLAIM = 5
MAX_TOTAL_SEARCH_RESULTS = 10
DEFAULT_SEARCH_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source_domain: str
    published_at: datetime | None
    retrieved_at: datetime
    trust_level: str

    def __post_init__(self):
        if self.trust_level not in ALLOWED_TRUST:
            raise ValueError(f"Invalid trust_level: {self.trust_level!r}")
        object.__setattr__(self, "title", str(self.title or ""))
        object.__setattr__(self, "url", str(self.url or ""))
        object.__setattr__(self, "snippet", str(self.snippet or ""))
        object.__setattr__(self, "source_domain", str(self.source_domain or ""))


@dataclass(frozen=True)
class EvidenceResult:
    claim: str
    status: str
    supporting_sources: tuple[str, ...]
    contradicting_sources: tuple[str, ...]
    confidence: float
    reason: str

    def __post_init__(self):
        if self.status not in ALLOWED_EVIDENCE_STATUS:
            raise ValueError(f"Invalid evidence status: {self.status!r}")
        score = float(self.confidence)
        if score < 0.0:
            score = 0.0
        if score > 1.0:
            score = 1.0
        object.__setattr__(self, "confidence", score)
        object.__setattr__(self, "supporting_sources", tuple(self.supporting_sources))
        object.__setattr__(self, "contradicting_sources", tuple(self.contradicting_sources))
        object.__setattr__(self, "claim", str(self.claim))
        object.__setattr__(self, "reason", str(self.reason))


@dataclass(frozen=True)
class ToolUsageRecord:
    tool_id: str
    task_id: str
    operation: str
    timestamp: datetime
    success: bool
    latency_ms: int
    metadata: Mapping[str, object] = None

    def __post_init__(self):
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata or {})),
        )
