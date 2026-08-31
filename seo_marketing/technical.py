"""Technical SEO analyzers (12.3) — deterministic over crawl snapshots."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from seo_marketing.platform_models import (
    DETERMINISTIC,
    InternalLinkRecommendation,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    SeoProvenance,
    StructuredDataFinding,
    TechnicalSeoAudit,
    TechnicalSeoIssue,
)

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
        h1 = str(page.get("h1") or "")
        if not h1.strip() and int(page.get("status_code") or 200) < 400:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="missing_h1",
                    severity="WARNING",
                    url=url,
                    reason="missing_h1",
                )
            )
        # Thin content — page-type-aware heuristic
        word_count = int(page.get("word_count") or 0)
        page_type = str(page.get("page_type") or "OTHER").upper()
        if page_type in {"ARTICLE", "LANDING"} and 0 < word_count < 150:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="thin_content_candidate",
                    severity="INFO",
                    url=url,
                    reason=f"word_count={word_count};page_type={page_type}",
                )
            )
        # Structured data presence (deterministic from snapshot fields)
        schemas = page.get("structured_data") or []
        if isinstance(schemas, list):
            for schema in schemas:
                stype = str(schema.get("type") or "Unknown")
                if schema.get("invalid"):
                    issues.append(
                        TechnicalSeoIssue(
                            issue_id=str(uuid.uuid4()),
                            code="structured_data_invalid",
                            severity="WARNING",
                            url=url,
                            reason=stype,
                        )
                    )
        # robots.txt path block evidence
        if page.get("robots_txt_disallow"):
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="robots_txt_blocked",
                    severity="WARNING",
                    url=url,
                    reason="robots_txt_disallow",
                )
            )
    # Sitemap membership analysis
    sitemap_urls = {str(u) for u in (pages[0].get("sitemap_urls") or [])} if pages else set()
    # Also accept top-level sitemap list via first page's site-level field
    for page in pages:
        for su in page.get("in_sitemap_urls") or []:
            sitemap_urls.add(str(su))
    page_urls = {str(p.get("url") or "") for p in pages}
    for page in pages:
        url = str(page.get("url") or "")
        if page.get("expect_in_sitemap") and url and url not in sitemap_urls and sitemap_urls:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="sitemap_missing_url",
                    severity="WARNING",
                    url=url,
                    reason="indexable_not_in_sitemap",
                )
            )
        if page.get("in_sitemap") and int(page.get("status_code") or 200) >= 400:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="sitemap_non_200",
                    severity="ERROR",
                    url=url,
                    reason="sitemap_url_error",
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


def analyze_structured_data(pages: list[dict]) -> list[StructuredDataFinding]:
    findings: list[StructuredDataFinding] = []
    known = {"Product", "Organization", "BreadcrumbList", "Article", "FAQPage", "WebSite", "LocalBusiness"}
    for page in pages:
        url = str(page.get("url") or "")
        schemas = page.get("structured_data") or []
        if not schemas:
            findings.append(
                StructuredDataFinding(
                    finding_id=str(uuid.uuid4()),
                    url=url,
                    schema_type="None",
                    present=False,
                    issues=("missing_structured_data",),
                    severity=SEVERITY_INFO,
                )
            )
            continue
        for schema in schemas:
            stype = str(schema.get("type") or "Unknown")
            issues: list[str] = []
            if stype not in known:
                issues.append("unknown_schema_type")
            if schema.get("invalid"):
                issues.append("invalid_markup")
            # Do not claim rich-result eligibility
            findings.append(
                StructuredDataFinding(
                    finding_id=str(uuid.uuid4()),
                    url=url,
                    schema_type=stype,
                    present=True,
                    issues=tuple(issues),
                    severity=SEVERITY_MEDIUM if issues else SEVERITY_INFO,
                )
            )
    return findings


def recommend_internal_links(
    *,
    tenant_id: str,
    site_id: str,
    pages: list[dict],
    links: list[dict] | None = None,
    max_recommendations: int = 20,
) -> list[InternalLinkRecommendation]:
    """Bounded link recommendations — never auto-insert."""
    inbound: dict[str, int] = {}
    outbound: dict[str, int] = {}
    for link in links or []:
        target = str(link.get("target") or "")
        source = str(link.get("source") or "")
        inbound[target] = inbound.get(target, 0) + 1
        outbound[source] = outbound.get(source, 0) + 1
    # Prefer linking from strong hubs (home/category) to orphan/weak pages
    hubs = [p for p in pages if p.get("is_home") or str(p.get("page_type") or "").upper() in {"HOME", "CATEGORY"}]
    weak = [p for p in pages if inbound.get(str(p.get("url") or ""), 0) == 0 and not p.get("is_home")]
    recs: list[InternalLinkRecommendation] = []
    for hub in hubs:
        for target in weak:
            if len(recs) >= max_recommendations:
                return recs
            src = str(hub.get("url") or "")
            tgt = str(target.get("url") or "")
            if not src or not tgt or src == tgt:
                continue
            anchor = str(target.get("title") or target.get("h1") or "related page")[:60]
            recs.append(
                InternalLinkRecommendation(
                    recommendation_id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    site_id=site_id,
                    source_url=src,
                    target_url=tgt,
                    suggested_anchor=anchor,
                    reason="orphan_or_weak_inbound",
                    confidence=0.6,
                    status="RECOMMENDATION_ONLY",
                )
            )
    return recs


def analyze_robots_txt(robots_body: str) -> list[dict]:
    """Parse robots evidence — compliance remains Data Acquisition concern."""
    findings: list[dict] = []
    if not robots_body.strip():
        findings.append({"code": "robots_missing", "severity": "WARNING", "reason": "empty_or_unreachable"})
        return findings
    lower = robots_body.casefold()
    if re.search(r"(?m)^\s*disallow:\s*/\s*$", lower) and not re.search(r"(?m)^\s*allow:\s*", lower):
        findings.append({"code": "robots_broad_disallow", "severity": "HIGH", "reason": "disallow_root"})
    if "sitemap:" not in lower:
        findings.append({"code": "robots_no_sitemap_ref", "severity": "INFO", "reason": "no_sitemap_directive"})
    if "eval(" in lower or "<script" in lower:
        findings.append({"code": "robots_malicious_content", "severity": "INFO", "reason": "untrusted_body_ignored"})
    return findings


def analyze_sitemap_entries(entries: list[dict]) -> list[TechnicalSeoIssue]:
    issues: list[TechnicalSeoIssue] = []
    seen: set[str] = set()
    for entry in entries:
        url = str(entry.get("url") or "")
        if not url:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="sitemap_invalid_url",
                    severity="ERROR",
                    url="",
                    reason="empty_url",
                )
            )
            continue
        if url in seen:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="sitemap_duplicate",
                    severity="WARNING",
                    url=url,
                    reason="duplicate",
                )
            )
        seen.add(url)
        status = int(entry.get("status_code") or 200)
        if status >= 400:
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="sitemap_non_200",
                    severity="ERROR",
                    url=url,
                    reason=str(status),
                )
            )
        if entry.get("noindex"):
            issues.append(
                TechnicalSeoIssue(
                    issue_id=str(uuid.uuid4()),
                    code="sitemap_noindex",
                    severity="WARNING",
                    url=url,
                    reason="noindex_in_sitemap",
                )
            )
    return issues
