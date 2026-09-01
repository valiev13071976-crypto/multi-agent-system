"""Deterministic intent + constraint extraction (LLM phrasing does not drive security)."""

from __future__ import annotations

import re
from decimal import Decimal

from business_assistant.models import (
    INTENT_ANALYZE,
    INTENT_COMMUNICATE,
    INTENT_COMPARE,
    INTENT_GENERATE,
    INTENT_MULTI_STEP,
    INTENT_PREPARE,
    INTENT_PUBLISH,
    INTENT_REPORT,
    INTENT_RESEARCH,
    INTENT_UPDATE,
    BusinessConstraint,
)


_INJECTION_PATTERNS = (
    r"ignore (all )?(policy|rules|instructions)",
    r"bypass (approval|hitl|policy)",
    r"publish all products",
    r"override (capability|permission)",
)


def detect_injection(text: str) -> bool:
    t = (text or "").casefold()
    return any(re.search(p, t) for p in _INJECTION_PATTERNS)


def classify_intent(text: str) -> str:
    t = (text or "").casefold()
    if any(w in t for w in ("только анализ", "только проанализируй", "analyze only", "read only", "ничего не меняй")):
        return INTENT_ANALYZE
    if any(w in t for w in ("сравни", "compare", "diff", "расхожден")):
        if any(w in t for w in ("договор", "document", "pdf", "docx")):
            return INTENT_COMPARE
        return INTENT_COMPARE
    if any(w in t for w in ("опубликуй", "publish", "публикац")):
        return INTENT_PUBLISH
    if any(w in t for w in ("подготов", "prepare", "покажи перед")):
        return INTENT_PREPARE
    if any(w in t for w in ("seo", "семант", "ключев")):
        return INTENT_RESEARCH
    if any(w in t for w in ("письм", "email", "ответ", "crm")):
        return INTENT_COMMUNICATE
    if any(w in t for w in ("отчёт", "report", "ежедневн")):
        return INTENT_REPORT
    if any(w in t for w in ("сгенерир", "контент", "описан", "generate")):
        return INTENT_GENERATE
    if any(w in t for w in ("обнов", "update", "sync", "синхрон")):
        return INTENT_UPDATE
    if any(w in t for w in ("прайс", "поставщик", "марж", "marketplace", "маркетплейс", "samsung", "bitrix")):
        return INTENT_MULTI_STEP
    return INTENT_ANALYZE


def extract_constraints(text: str) -> BusinessConstraint:
    t = text or ""
    tl = t.casefold()
    brands: list[str] = []
    for brand in ("Samsung", "Apple", "Xiaomi", "Acme"):
        if brand.casefold() in tl:
            brands.append(brand)
    marketplaces: list[str] = []
    if "ozon" in tl:
        marketplaces.append("OZON")
    if "wb" in tl or "wildberries" in tl or "вайлдберриз" in tl:
        marketplaces.append("WILDBERRIES")
    if "яндекс" in tl or "yandex" in tl:
        marketplaces.append("YANDEX_MARKET")
    channels: list[str] = []
    if any(w in tl for w in ("сайт", "bitrix", "aspro", "website")):
        channels.append("BITRIX")
    top_n = None
    m = re.search(r"(?:лучш\w*|top|первые)\s+(\d+)", tl)
    if m:
        top_n = int(m.group(1))
    margin = None
    mm = re.search(r"марж[а-я]*\s*[>=]?\s*(\d+(?:[.,]\d+)?)\s*%?", tl)
    if mm:
        margin = Decimal(mm.group(1).replace(",", "."))
    read_only = any(w in tl for w in ("только анализ", "только проанализируй", "ничего не меняй", "read only", "analyze only"))
    show_before = any(w in tl for w in ("покажи перед", "show me before", "перед публикац", "preview before"))
    unknown: list[str] = []
    if "бюджет" in tl and not re.search(r"бюджет\s+\d+", tl):
        unknown.append("budget")
    return BusinessConstraint(
        brands=tuple(brands),
        marketplaces=tuple(marketplaces),
        channels=tuple(channels),
        margin_min_pct=margin,
        top_n=top_n,
        read_only=read_only,
        show_before_publication=show_before,
        unknown=tuple(unknown),
    )


def objective_from(text: str, intent: str) -> str:
    return f"{intent}: {(text or '').strip()[:240]}"
