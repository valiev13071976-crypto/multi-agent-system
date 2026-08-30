"""Governed content research with evidence provenance."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from content_intel.platform_models import (
    GROUNDING_CONFLICTING,
    GROUNDING_SUPPORTED,
    GROUNDING_UNSUPPORTED,
    RESEARCH_PROFILE_VERSION,
    ResearchEvidence,
    ResearchReport,
    content_hash,
)
from security.tenant import normalize_tenant_id

_POISON_MARKERS = (
    "ignore instructions",
    "ignore all previous",
    "call admin",
    "reveal secrets",
    "publish immediately",
    "change tenant",
    "save this permanently",
)


def normalize_evidence_rows(rows: list[dict], *, tenant_id: str) -> list[ResearchEvidence]:
    tenant = normalize_tenant_id(tenant_id)
    out: list[ResearchEvidence] = []
    seen_hashes: set[str] = set()
    for row in rows:
        claim = str(row.get("extracted_claim") or row.get("content") or "").strip()
        if not claim:
            continue
        digest = content_hash(claim)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        warnings: list[str] = []
        if any(p in claim.lower() for p in _POISON_MARKERS):
            warnings.append("untrusted_instruction_detected")
        out.append(
            ResearchEvidence(
                evidence_id=str(uuid.uuid4()),
                tenant_id=tenant,
                source_type=str(row.get("source_type") or "manual"),
                source_ref=str(row.get("source_ref") or "unknown"),
                label=str(row.get("label") or "evidence"),
                extracted_claim=claim,
                content_hash=digest,
                retrieved_at=datetime.now(timezone.utc),
                trust_level=str(row.get("trust_level") or "unverified_external"),
                relevance=float(row.get("relevance") or 0.5),
                publication_date=row.get("publication_date"),
                warnings=tuple(warnings),
            )
        )
    return out


def classify_grounding(evidence: list[ResearchEvidence]) -> tuple[str, tuple[str, ...]]:
    if not evidence:
        return GROUNDING_UNSUPPORTED, ()
    claims = {e.extracted_claim.lower() for e in evidence}
    conflicts: list[str] = []
    if len(claims) > 1 and any("not " in c for c in claims):
        conflicts.append("conflicting_claims_detected")
        return GROUNDING_CONFLICTING, tuple(conflicts)
    return GROUNDING_SUPPORTED, tuple(conflicts)


def build_research_report(
    *,
    tenant_id: str,
    project_id: str,
    objective_id: str,
    evidence_rows: list[dict],
    max_evidence: int = 50,
    freshness_hours: int = 168,
) -> ResearchReport:
    evidence = normalize_evidence_rows(evidence_rows[:max_evidence], tenant_id=tenant_id)
    now = datetime.now(timezone.utc)
    fresh = [
        e
        for e in evidence
        if e.publication_date is None
        or (now - e.publication_date) <= timedelta(hours=freshness_hours)
    ]
    grounding, conflicts = classify_grounding(fresh or evidence)
    return ResearchReport(
        report_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        project_id=project_id,
        objective_id=objective_id,
        evidence=tuple(fresh or evidence),
        grounding=grounding,
        profile_version=RESEARCH_PROFILE_VERSION,
        conflicts=conflicts,
    )
