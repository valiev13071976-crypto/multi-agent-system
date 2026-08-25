from datetime import datetime, timezone
import re

from tools.models import (
    EVIDENCE_CONTRADICTED,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_SUPPORTED,
    EVIDENCE_UNKNOWN,
    MAX_FACT_CLAIMS,
    TRUST_HIGH,
    TRUST_MEDIUM,
    EvidenceResult,
    SearchResult,
)
from tools.url_safety import normalize_url_for_dedup, source_domain


FACT_HINT_RE = re.compile(
    r"\d|%|процент|\b(?:19|20)\d{2}\b|"
    r"\b(?:gdp|revenue|population|индекс|доля|рост|вырос|снизил|упал)\b",
    re.I,
)
TOKEN_RE = re.compile(r"[a-zа-яё0-9%]{3,}", re.I)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "are",
        "was",
        "were",
        "для",
        "что",
        "это",
        "как",
        "или",
        "при",
        "над",
    }
)
POLAR_PAIRS = (
    ("increase", "decrease"),
    ("increased", "decreased"),
    ("вырос", "упал"),
    ("рост", "падение"),
    ("higher", "lower"),
)
TRUSTED_ENOUGH = frozenset({TRUST_MEDIUM, TRUST_HIGH})


def extract_claims(texts: list[str], *, limit: int = MAX_FACT_CLAIMS) -> tuple[str, ...]:
    claims = []
    seen = set()
    for text in texts:
        for part in re.split(r"(?<=[.!?])\s+", str(text or "").strip()):
            candidate = " ".join(part.split()).strip()
            if len(candidate) < 12:
                continue
            if not FACT_HINT_RE.search(candidate):
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            claims.append(candidate)
            if len(claims) >= limit:
                return tuple(claims)
    return tuple(claims)


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in TOKEN_RE.findall(text or "")
        if token.casefold() not in STOPWORDS
    }


def _numbers(text: str) -> set[str]:
    return {item.replace(",", ".") for item in NUMBER_RE.findall(text or "")}


def _has_polar_conflict(left: str, right: str) -> bool:
    a = left.casefold()
    b = right.casefold()
    for one, two in POLAR_PAIRS:
        if (one in a and two in b) or (two in a and one in b):
            return True
    return False


def trust_allows_support(trust_level: str) -> bool:
    return trust_level in TRUSTED_ENOUGH


def dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    seen_urls = set()
    seen_domain_title = set()
    unique = []
    for item in results:
        try:
            url_key = normalize_url_for_dedup(item.url)
        except Exception:
            continue
        domain = source_domain(item.url) or item.source_domain
        title_key = (domain, item.title.strip().casefold())
        if url_key in seen_urls or title_key in seen_domain_title:
            continue
        seen_urls.add(url_key)
        seen_domain_title.add(title_key)
        unique.append(item)
    return unique


def independent_domains(results: list[SearchResult]) -> tuple[str, ...]:
    domains = []
    seen = set()
    for item in results:
        domain = source_domain(item.url) or item.source_domain
        if not domain or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return tuple(domains)


def _result_text(item: SearchResult) -> str:
    return f"{item.title} {item.snippet}"


def classify_result(claim: str, item: SearchResult) -> str:
    haystack = _result_text(item)
    claim_tokens = _tokens(claim)
    result_tokens = _tokens(haystack)
    overlap = claim_tokens & result_tokens
    claim_numbers = _numbers(claim)
    result_numbers = _numbers(haystack)
    if _has_polar_conflict(claim, haystack):
        return "contradict"
    if claim_numbers and result_numbers and claim_numbers.isdisjoint(result_numbers) and overlap:
        return "contradict"
    if claim_numbers and claim_numbers <= result_numbers:
        return "support"
    if len(overlap) >= 2:
        return "support"
    return "none"


def match_claim(claim: str, results: list[SearchResult]) -> EvidenceResult:
    unique = dedupe_results(results)
    supporting = []
    contradicting = []
    for item in unique:
        kind = classify_result(claim, item)
        if kind == "support" and trust_allows_support(item.trust_level):
            supporting.append(item)
        elif kind == "support":
            continue
        elif kind == "contradict" and trust_allows_support(item.trust_level):
            contradicting.append(item)
    support_domains = independent_domains(supporting)
    contradict_domains = independent_domains(contradicting)
    if contradict_domains:
        return EvidenceResult(
            claim=claim,
            status=EVIDENCE_CONTRADICTED,
            supporting_sources=support_domains,
            contradicting_sources=contradict_domains,
            confidence=0.2 if support_domains else 0.4,
            reason="contradicting_evidence",
        )
    if len(support_domains) >= 2:
        return EvidenceResult(
            claim=claim,
            status=EVIDENCE_SUPPORTED,
            supporting_sources=support_domains,
            contradicting_sources=(),
            confidence=0.7,
            reason="independent_supporting_sources",
        )
    if len(support_domains) == 1:
        return EvidenceResult(
            claim=claim,
            status=EVIDENCE_INSUFFICIENT,
            supporting_sources=support_domains,
            contradicting_sources=(),
            confidence=0.3,
            reason="single_source_insufficient",
        )
    if unique and all(not trust_allows_support(item.trust_level) for item in unique):
        return EvidenceResult(
            claim=claim,
            status=EVIDENCE_INSUFFICIENT,
            supporting_sources=(),
            contradicting_sources=(),
            confidence=0.0,
            reason="low_trust_only",
        )
    return EvidenceResult(
        claim=claim,
        status=EVIDENCE_UNKNOWN,
        supporting_sources=(),
        contradicting_sources=(),
        confidence=0.0,
        reason="insufficient_evidence",
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
