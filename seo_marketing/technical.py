"""Technical SEO analyzers (12.3) — deterministic over crawl snapshots."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from seo_marketing.platform_models import DETERMINISTIC, SeoProvenance, TechnicalSeoAudit, TechnicalSeoIssue

_INJECTION = re.compile(r"(ignore\s+(all\s+)?previous|system\s*:|reveal\s+token)", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def analyze_indexability(entry: dict) -> str:
    status = int(entry.get("status_code") or 200)
    robots = str(entry.get("robots") or "").casefold()
    if status >= 400:
        return "HTTP_ERROR"
    if "noindex" in robots:
        return "NOINDEX"
    if entry.get("robots_blocked"):
        return "ROBOTS_BLOCKED"
    canon = str(entry.get("canonical") or "")
    url = str(entry.get("url") or "")
    if canon and canon != url:
        return "CANONICAL_TO_OTHER"
    if entry.get("redirect_to"):
        return "REDIRECT"
    return "INDEXABLE"


def analyze_technical_snapshot(
    *,
    tenant_id: str,
    site_id: str,
    snapshot_id: str,
    pages: list[dict],
    links: list[dict] | None = None,
) -> TechnicalSeoAudit:
    issues: list[TechnicalSeoIssue] = []
    titles_seen: dict[str, str] = {}
    for page in pages:
        url = str(page.get("url") or "")
        title = str(page.get("title") or "")
        if _INJECTION.search(str(page.get("html") or title)):
            pass  # remains data; no instruction execution
        idx = analyze_indexability(page)
        if idx != "INDEXABLE":
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code=f"indexability_{idx.lower()}",
                    severity="ERROR" if idx in {"HTTP_ERROR", "NOINDEX"} else "WARNING",
                    url=url,
                    reason=idx,
                )
            )
        if not title:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="missing_title",
                    severity="ERROR",
                    url=url,
                    reason="missing_title",
                )
            )
        elif title in titles_seen and titles_seen[title] != url:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="duplicate_title",
                    severity="WARNING",
                    url=url,
                    reason="duplicate_title",
                )
            )
        else:
            titles_seen[title] = url
        canon = str(page.get("canonical") or "")
        if page.get("canonical_conflict"):
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="canonical_conflict",
                    severity="ERROR",
                    url=url,
                    reason="multiple_canonical",
                )
            )
        if page.get("redirect_chain_len", 0) > 2:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="redirect_chain",
                    severity="WARNING",
                    url=url,
                    reason="redirect_chain",
                )
            )
    inbound: dict[str, int] = {}
    for link in links or []:
        target = str(link.get("target") or "")
        inbound[target] = inbound.get(target, 0) + 1
    for page in pages:
        url = str(page.get("url") or "")
        if inbound.get(url, 0) == 0 and not page.get("is_home"):
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="orphan_candidate",
                    severity="INFO",
                    url=url,
                    reason="no_inbound_internal_links",
                )
            )
    return TechnicalSeoAudit(
        audit_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        site_id=site_id,
        snapshot_id=snapshot_id,
        issues=tuple(issues),
        url_count=len(pages),
        provenance=SeoProvenance(
            source="technical_audit",
            observed_at=_utc(),
            retrieved_at=_utc(),
            trust_level=DETERMINISTIC,
        ),
    )
