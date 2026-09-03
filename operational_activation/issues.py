"""Issue taxonomy for real pilot UX/business loop — no preference redesign."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from operational_activation.status import ENGINEERING_READY, WAITING_FOR_EVIDENCE


SEVERITY_P0 = "P0"  # security/data loss
SEVERITY_P1 = "P1"  # blocked core workflow
SEVERITY_P2 = "P2"  # material UX/business friction
SEVERITY_P3 = "P3"  # improvement


@dataclass
class ProductIssue:
    issue_id: str
    severity: str
    title: str
    source: str
    reproducibility: str
    affected_users: str
    affected_workflow: str
    metrics_or_logs: str
    proposed_minimum_fix: str
    status: str = "OPEN"


@dataclass
class IssueBoard:
    issues: list[ProductIssue] = field(default_factory=list)

    def add(self, issue: ProductIssue) -> None:
        self.issues.append(issue)

    def counts(self) -> dict[str, int]:
        out = {SEVERITY_P0: 0, SEVERITY_P1: 0, SEVERITY_P2: 0, SEVERITY_P3: 0, "fixed": 0}
        for i in self.issues:
            if i.status == "CLOSED":
                out["fixed"] += 1
            if i.severity in out:
                out[i.severity] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        real = [i for i in self.issues if i.source != "synthetic"]
        return {
            "status": WAITING_FOR_EVIDENCE if not real else ENGINEERING_READY,
            "real_issues_count": len(real),
            "real_fixes_count": sum(1 for i in real if i.status == "CLOSED"),
            "taxonomy": [SEVERITY_P0, SEVERITY_P1, SEVERITY_P2, SEVERITY_P3],
            "required_evidence_fields": [
                "source",
                "reproducibility",
                "affected_users",
                "affected_workflow",
                "severity",
                "metrics_or_logs",
                "proposed_minimum_fix",
            ],
            "counts": self.counts(),
            "issues": [
                {
                    "issue_id": i.issue_id,
                    "severity": i.severity,
                    "title": i.title,
                    "source": i.source,
                    "status": i.status,
                }
                for i in self.issues
            ],
            "note": "Only real pilot evidence may create production-priority issues",
        }


DEFAULT_ISSUE_BOARD = IssueBoard()
