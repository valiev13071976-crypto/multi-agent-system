"""Keyword research intelligence (12.1)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from seo_marketing.platform_models import (
    INTENT_UNKNOWN,
    Keyword,
    KeywordCluster,
    KeywordMetric,
    KeywordOpportunity,
    KeywordPageMapping,
    MAPPING_AMBIGUOUS,
    MAPPING_CANDIDATE,
    MAPPING_CONFIRMED,
    MAPPING_UNMAPPED,
    MODEL_INFERRED,
    NOT_AVAILABLE,
    NORMALIZED,
    SeoProvenance,
    TRUSTED_EXTERNAL,
)

_INJECTION = re.compile(r"(ignore\s+(all\s+)?previous|system\s*:|reveal\s+token|delete\s+catalog)", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_keyword(text: str) -> str:
    raw = " ".join(str(text or "").strip().split())
    return raw.casefold()


def normalize_keywords(rows: list[dict], *, tenant_id: str, site_id: str, source: str) -> list[Keyword]:
    seen: set[str] = set()
    out: list[Keyword] = []
    for row in rows:
        text = str(row.get("text") or row.get("query") or "").strip()
        if not text:
            continue
        norm = normalize_keyword(text)
        if norm in seen:
            continue
        seen.add(norm)
        kid = str(row.get("keyword_id") or uuid.uuid4())
        prov = SeoProvenance(
            source=source,
            observed_at=str(row.get("observed_at") or _utc()),
            retrieved_at=_utc(),
            trust_level=TRUSTED_EXTERNAL if source == "search_console" else NORMALIZED,
            source_version=str(row.get("source_version") or ""),
        )
        out.append(
            Keyword(
                keyword_id=kid,
                tenant_id=tenant_id,
                site_id=site_id,
                text=text,
                normalized=norm,
                source=source,
                provenance=prov,
            )
        )
    return out


def keyword_metrics_from_row(keyword_id: str, row: dict, *, source: str) -> list[KeywordMetric]:
    prov = SeoProvenance(source=source, observed_at=_utc(), retrieved_at=_utc(), trust_level=TRUSTED_EXTERNAL)
    metrics: list[KeywordMetric] = []
    for name in ("impressions", "clicks", "ctr", "position"):
        if name in row and row[name] is not None:
            metrics.append(
                KeywordMetric(
                    keyword_id=keyword_id,
                    metric=name,
                    value=row[name],
                    unit="count" if name != "ctr" else "ratio",
                    trust_level=TRUSTED_EXTERNAL,
                    provenance=prov,
                )
            )
    volume = row.get("search_volume")
    if volume is None:
        metrics.append(
            KeywordMetric(
                keyword_id=keyword_id,
                metric="search_volume",
                value=NOT_AVAILABLE,
                unit="count",
                trust_level=NOT_AVAILABLE,
                provenance=prov,
            )
        )
    elif source == "trusted_provider":
        metrics.append(
            KeywordMetric(
                keyword_id=keyword_id,
                metric="search_volume",
                value=volume,
                unit="count",
                trust_level=TRUSTED_EXTERNAL,
                provenance=prov,
            )
        )
    return metrics


def classify_intent(text: str, *, model_output: dict | None = None) -> tuple[str, str, float]:
    if model_output and model_output.get("intent"):
        return str(model_output["intent"]), MODEL_INFERRED, float(model_output.get("confidence") or 0.5)
    lower = text.casefold()
    if any(w in lower for w in ("buy", "price", "order", "shop")):
        return "TRANSACTIONAL", MODEL_INFERRED, 0.6
    if any(w in lower for w in ("near me", "address")):
        return "LOCAL", MODEL_INFERRED, 0.55
    if any(w in lower for w in ("best", "review", "compare")):
        return "COMMERCIAL", MODEL_INFERRED, 0.55
    if any(w in lower for w in ("how", "what", "guide")):
        return "INFORMATIONAL", MODEL_INFERRED, 0.55
    return INTENT_UNKNOWN, MODEL_INFERRED, 0.3


def cluster_keywords(keywords: list[Keyword], *, tenant_id: str, site_id: str) -> list[KeywordCluster]:
    buckets: dict[str, list[str]] = {}
    for kw in keywords:
        prefix = kw.normalized.split(" ")[0] if kw.normalized else "misc"
        buckets.setdefault(prefix, []).append(kw.keyword_id)
    clusters: list[KeywordCluster] = []
    for label, kids in buckets.items():
        intent, trust, _ = classify_intent(label)
        clusters.append(
            KeywordCluster(
                cluster_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                site_id=site_id,
                label=label,
                keyword_ids=tuple(kids),
                intent=intent,
                trust_level=trust,
                provenance=SeoProvenance(source="cluster", observed_at=_utc(), retrieved_at=_utc(), trust_level=NORMALIZED),
            )
        )
    return clusters


def score_opportunity(
    keyword: Keyword,
    *,
    metrics: list[KeywordMetric],
    page_mappings: list[KeywordPageMapping],
) -> KeywordOpportunity:
    components: list[str] = []
    score = 0.0
    impressions = next((m.value for m in metrics if m.metric == "impressions"), 0)
    ctr = next((m.value for m in metrics if m.metric == "ctr"), None)
    position = next((m.value for m in metrics if m.metric == "position"), None)
    if impressions and float(impressions) >= 100:
        components.append("HIGH_IMPRESSIONS")
        score += 0.3
    if ctr is not None and float(ctr) < 0.03:
        components.append("LOW_CTR")
        score += 0.2
    if position is not None and 4 <= float(position) <= 10:
        components.append("POSITION_4_10")
        score += 0.2
    mapped = [m for m in page_mappings if m.keyword_id == keyword.keyword_id]
    if not mapped or all(m.state == MAPPING_UNMAPPED for m in mapped):
        components.append("NO_DEDICATED_PAGE")
        score += 0.3
    return KeywordOpportunity(
        opportunity_id=str(uuid.uuid4()),
        tenant_id=keyword.tenant_id,
        keyword_id=keyword.keyword_id,
        score=min(score, 1.0),
        components=tuple(components),
        trust_level=NORMALIZED,
    )


def map_keyword_to_pages(
    keyword: Keyword,
    pages: list[dict],
) -> KeywordPageMapping:
    matches = []
    for page in pages:
        url = str(page.get("url") or "").casefold()
        title = str(page.get("title") or "").casefold()
        if keyword.normalized in url or keyword.normalized in title:
            matches.append(str(page.get("page_id") or ""))
    if len(matches) == 1:
        state = MAPPING_CONFIRMED
    elif len(matches) > 1:
        state = MAPPING_AMBIGUOUS
    elif len(matches) == 0:
        state = MAPPING_UNMAPPED
        matches = [""]
    else:
        state = MAPPING_CANDIDATE
    return KeywordPageMapping(
        mapping_id=str(uuid.uuid4()),
        tenant_id=keyword.tenant_id,
        keyword_id=keyword.keyword_id,
        page_id=matches[0] if matches else "",
        state=state,
        evidence=tuple(matches),
    )


def detect_cannibalization(mappings: list[KeywordPageMapping]) -> list[tuple[str, str]]:
    page_to_keywords: dict[str, list[str]] = {}
    for m in mappings:
        if m.page_id:
            page_to_keywords.setdefault(m.page_id, []).append(m.keyword_id)
    return [(pid, ",".join(kids)) for pid, kids in page_to_keywords.items() if len(kids) > 2]


def sanitize_untrusted_keyword(text: str) -> tuple[str, bool]:
    if _INJECTION.search(text):
        return text, True
    return text, False
