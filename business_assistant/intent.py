"""Intent + constraint extraction — separates conversation from business integration actions."""

from __future__ import annotations

import re
from decimal import Decimal

from agents.task_classifier import (
    CATEGORY_CRITIQUE,
    CATEGORY_GENERAL,
    CATEGORY_RESEARCH,
    CATEGORY_STRATEGY,
    CATEGORY_TECHNICAL,
    CATEGORY_TREND_ANALYSIS,
    classify_task,
)
from business_assistant.models import (
    INTENT_ANALYZE,
    INTENT_COMMUNICATE,
    INTENT_COMPARE,
    INTENT_CONVERSATIONAL,
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

_DOMAIN_TERMS = (
    "ozon",
    "озон",
    "1c",
    "1с",
    "bitrix",
    "битрикс",
    "wildberries",
    " wb",
    "wb ",
    "вайлдберриз",
    "яндекс",
    "yandex",
    "crm",
    "email",
    "календар",
    "calendar",
)

_ACTION_VERBS = (
    "покажи",
    "show me",
    "show ",
    "проверь",
    "check ",
    "найди",
    "find ",
    "получи",
    "get ",
    "выведи",
    "fetch",
    "загрузи",
    "load ",
    "синхрониз",
    "sync ",
    "обнови",
    "update ",
    "измени",
    "change ",
    "установ",
    "set ",
    "опубликуй",
    "publish",
    "отправ",
    "send ",
    "импорт",
    "import ",
    "экспорт",
    "export ",
    "подготовь измен",
    "prepare update",
)

_DATA_REQUEST_PATTERNS = (
    r"\b(остат(ок|ки|ов)|stock)\b",
    r"текущ(ая|ую|ие|ий)\s+(цен|остат|комис|данн|товар)",
    r"current\s+(price|stock|commission|data|product)",
    r"\bsku[-\s]?\w+",
    r"артикул",
    r"мо(й|я|и|их|ём)\s+(заказ|остат|цен|комис|данн|товар|комисс)",
    r"my\s+(order|stock|price|commission|data|product)",
)

_BUSINESS_TASK_KEYWORDS = (
    "прайс",
    "поставщик",
    "supplier",
    "samsung",
    "марж",
    "excel",
    "xlsx",
    "csv",
    "таблиц",
    "отчёт постав",
    "supplier price",
    "marketplace profit",
)


def detect_injection(text: str) -> bool:
    t = (text or "").casefold()
    return any(re.search(p, t) for p in _INJECTION_PATTERNS)


def requires_business_integration(text: str) -> bool:
    """True when the user requests business data retrieval or governed external action."""
    raw = (text or "").strip()
    if not raw:
        return False
    tl = raw.casefold()

    if any(w in tl for w in ("измени", "change price", "установ", "опубликуй", "publish all")):
        return True

    has_action = any(v in tl for v in _ACTION_VERBS)
    has_domain = any(d in tl for d in _DOMAIN_TERMS)
    has_data = any(re.search(p, tl) for p in _DATA_REQUEST_PATTERNS)

    if has_action and (has_data or has_domain):
        return True
    if has_data and has_domain:
        return True
    if any(k in tl for k in _BUSINESS_TASK_KEYWORDS):
        return True

    if any(w in tl for w in ("только анализ", "analyze only", "read only")) and any(
        w in tl for w in ("прайс", "поставщик", "samsung", "ozon", "marketplace")
    ):
        return True
    return False


def is_conversational(text: str, *, has_attachments: bool = False) -> bool:
    """General knowledge / reasoning / chat — not business data/action integration."""
    raw = (text or "").strip()
    if not raw:
        return False

    tl = raw.casefold()

    # Analytical discussion about domains/tools — not integration by itself.
    if re.search(r"\b(объясни|explain|расскажи|tell me about|как работает|how does|how do)\b", tl):
        return True
    if re.search(r"(плюс.*минус|pros and cons|преимуществ|недостатк|advantages and disadvantages)", tl):
        return True
    if re.search(r"\b(какие риски|what are the risks|риски при)\b", tl):
        return True
    if re.search(r"\b(привет|здравств|hello|hi|hey)\b", tl) or tl.startswith("добрый"):
        return True
    if re.search(r"как\s+(ты|дела|поживаешь)", tl) or "how are you" in tl:
        return True
    if re.search(r"\b(кто\s+ты|who\s+are\s+you|what\s+are\s+you)\b", tl):
        return True
    if "что ты умеешь" in tl or "what can you do" in tl:
        return True

    # Production defect closure (XLSX attachment -> failed response): a
    # request that carries a file attachment (spreadsheet/document) must
    # reach the conversational Panda AI core -- the ONLY pipeline that
    # actually resolves attachments and reads their content (see
    # WorkflowPandaConversationGateway.respond()'s spreadsheet_attachment_count
    # / detect_family(has_spreadsheet_attachment=...) in
    # business_assistant/action_continuation.py). The older fixture-only
    # business-workflow/recipe engine below (BusinessAssistantService.execute)
    # never consumes artifact_refs at all, so previously any attachment
    # turn whose wording also matched a business/domain term (e.g.
    # mentioning "Bitrix" while asking Panda to analyze an uploaded price
    # list) got misrouted there and silently never read the file. An
    # explicit immediate-write/publish verb (already gating
    # requires_business_integration below) still overrides this and stays
    # on the governed business-workflow path unchanged.
    if has_attachments and not any(
        w in tl for w in ("измени", "change price", "установ", "опубликуй", "publish all")
    ):
        return True

    # Production defect closure (product-card enrichment request routed to
    # the degraded generic business-workflow recipe engine instead of
    # WorkflowPandaConversationGateway's CALL_PRODUCT_ENRICHMENT): an
    # explicit "Подготовь полную карточку товара ... для Bitrix/Aspro ..."
    # request mentions a domain term ("Bitrix"/"Aspro") and an action verb
    # ("подготовь"), so requires_business_integration() below returns True
    # for it whenever this turn has no artifact_refs attached (e.g. the
    # file was uploaded on an EARLIER turn, per "из загруженного прайса").
    # That routed it into BusinessAssistantService.execute()'s fixture
    # SUPPLIER_PRICE recipe, which immediately blocks on Bitrix
    # integration capabilities it never needed (product_enrichment never
    # writes to Bitrix by itself -- see is_explicit_product_enrichment_
    # request's own docstring), producing the reported
    # BA_CAPABILITY_UNAVAILABLE/dependency_not_ready-degraded generic
    # result instead of the enrichment preview. Local import avoids a
    # circular import (action_continuation already imports
    # requires_business_integration from this module).
    from business_assistant.action_continuation import (
        is_bitrix_write_plan_question,
        is_explicit_product_enrichment_request,
    )

    if is_explicit_product_enrichment_request(raw):
        return True

    # Production defect closure (same misrouting, read-only follow-up
    # variant): "Покажи точно, какие данные из этой карточки товара будут
    # записаны в Bitrix/Aspro, если я подтвержу запись ... Ничего в Bitrix
    # не записывай." also mentions "Bitrix" plus an action verb, so
    # requires_business_integration below sent it to the same degraded
    # recipe engine, whose reply merely echoed the instruction text back.
    # It must reach WorkflowPandaConversationGateway, which answers it from
    # the prepared card state (EXPLAIN_BITRIX_WRITE_PLAN). Never a write:
    # the predicate refuses any explicit write confirmation.
    if is_bitrix_write_plan_question(raw):
        return True

    if requires_business_integration(raw):
        return False

    classification = classify_task(raw)
    if classification.category in {
        CATEGORY_GENERAL,
        CATEGORY_STRATEGY,
        CATEGORY_CRITIQUE,
        CATEGORY_RESEARCH,
        CATEGORY_TREND_ANALYSIS,
        CATEGORY_TECHNICAL,
    }:
        return True
    return False


def classify_intent(text: str, *, has_attachments: bool = False) -> str:
    if is_conversational(text, has_attachments=has_attachments):
        return INTENT_CONVERSATIONAL
    t = (text or "").casefold()
    if any(w in t for w in ("только анализ", "только проанализируй", "analyze only", "read only", "ничего не меняй")):
        return INTENT_ANALYZE
    if any(w in t for w in ("сравни", "compare", "diff", "расхожден")):
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
    if any(w in t for w in ("обнов", "update", "sync", "синхрон", "измени", "change price")):
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
