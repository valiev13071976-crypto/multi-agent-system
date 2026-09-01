"""Recipe templates — planning aids, not a second workflow engine."""

from __future__ import annotations

from business_assistant.models import (
    CAP_CMS_BITRIX,
    CAP_COMMERCE,
    CAP_CONTENT,
    CAP_DATA_COMPARE,
    CAP_DATA_INGEST,
    CAP_DATA_MATCH,
    CAP_DATA_NORMALIZE,
    CAP_DOC_COMPARE,
    CAP_EMAIL,
    CAP_ERP_1C,
    CAP_MARKETPLACE,
    CAP_MEDIA,
    CAP_SEO,
    RECIPE_COMMUNICATION,
    RECIPE_DAILY_REPORT,
    RECIPE_DOCUMENT_COMPARE,
    RECIPE_GENERIC,
    RECIPE_MARKETPLACE_PROFIT,
    RECIPE_ONEC_PRICE,
    RECIPE_PRODUCT_LAUNCH,
    RECIPE_SEO_REVIEW,
    RECIPE_SUPPLIER_PRICE,
    STEP_ANALYZE,
    STEP_GENERATE,
    STEP_PREPARE_WRITE,
    STEP_READ,
    STEP_VERIFY,
    STEP_WRITE,
    BusinessConstraint,
    BusinessPlanStep,
)


def _step(sid: str, name: str, cap: str, klass: str, deps: tuple[str, ...] = (), **kw) -> BusinessPlanStep:
    return BusinessPlanStep(
        step_id=sid,
        name=name,
        capability=cap,
        step_class=klass,
        depends_on=deps,
        **kw,
    )


def supplier_price_steps(*, constraints: BusinessConstraint, publish: bool) -> list[BusinessPlanStep]:
    steps = [
        _step("s1", "ingest_supplier_price", CAP_DATA_INGEST, STEP_READ, workload="batch"),
        _step("s2", "normalize_price_rows", CAP_DATA_NORMALIZE, STEP_ANALYZE, ("s1",), workload="batch"),
        _step("s3", "match_products", CAP_DATA_MATCH, STEP_ANALYZE, ("s2",)),
        _step("s4", "compare_previous_prices", CAP_DATA_COMPARE, STEP_ANALYZE, ("s3",)),
        _step("s5", "marketplace_economics", CAP_MARKETPLACE, STEP_ANALYZE, ("s4",)),
        _step("s6", "rank_profitable_candidates", CAP_COMMERCE, STEP_ANALYZE, ("s5",)),
        _step("s7", "prepare_content", CAP_CONTENT, STEP_GENERATE, ("s6",)),
        _step("s8", "prepare_media", CAP_MEDIA, STEP_GENERATE, ("s7",)),
        _step("s9", "prepare_seo", CAP_SEO, STEP_GENERATE, ("s8",)),
        _step("s10", "prepare_site_publication", CAP_CMS_BITRIX, STEP_PREPARE_WRITE, ("s9",), risk_level="HIGH", requires_approval=True),
        _step("s11", "prepare_marketplace_publication", CAP_MARKETPLACE, STEP_PREPARE_WRITE, ("s9",), risk_level="HIGH", requires_approval=True),
    ]
    if publish and not constraints.read_only and not constraints.show_before_publication:
        steps.append(
            _step(
                "s12",
                "apply_publication",
                CAP_CMS_BITRIX,
                STEP_WRITE,
                ("s10", "s11"),
                risk_level="CRITICAL",
                requires_approval=True,
            )
        )
        steps.append(_step("s13", "verify_publication", CAP_CMS_BITRIX, STEP_VERIFY, ("s12",)))
    else:
        # Explicit stop before publication
        steps.append(_step("s12", "preview_and_wait_approval", CAP_CMS_BITRIX, STEP_PREPARE_WRITE, ("s10", "s11"), risk_level="HIGH", requires_approval=True))
    return steps


def product_launch_steps(*, constraints: BusinessConstraint) -> list[BusinessPlanStep]:
    return [
        _step("p1", "validate_product_facts", CAP_COMMERCE, STEP_READ),
        _step("p2", "content_handoff", CAP_CONTENT, STEP_GENERATE, ("p1",)),
        _step("p3", "media_handoff", CAP_MEDIA, STEP_GENERATE, ("p2",)),
        _step("p4", "seo_handoff", CAP_SEO, STEP_GENERATE, ("p3",)),
        _step("p5", "pricing_prepare", CAP_COMMERCE, STEP_ANALYZE, ("p1",)),
        _step("p6", "marketplace_selection", CAP_MARKETPLACE, STEP_PREPARE_WRITE, ("p5",), requires_approval=True, risk_level="HIGH"),
        _step("p7", "site_sync_preview", CAP_CMS_BITRIX, STEP_PREPARE_WRITE, ("p4", "p5"), requires_approval=True, risk_level="HIGH"),
    ]


def marketplace_profit_steps() -> list[BusinessPlanStep]:
    return [
        _step("m1", "read_listings", CAP_MARKETPLACE, STEP_READ),
        _step("m2", "economics_scan", CAP_MARKETPLACE, STEP_ANALYZE, ("m1",)),
        _step("m3", "loss_detection", CAP_MARKETPLACE, STEP_ANALYZE, ("m2",)),
        _step("m4", "propose_corrections", CAP_MARKETPLACE, STEP_PREPARE_WRITE, ("m3",), requires_approval=True, risk_level="HIGH"),
    ]


def seo_review_steps() -> list[BusinessPlanStep]:
    return [
        _step("seo1", "seo_snapshot", CAP_SEO, STEP_READ),
        _step("seo2", "opportunities", CAP_SEO, STEP_ANALYZE, ("seo1",)),
        _step("seo3", "content_briefs", CAP_CONTENT, STEP_GENERATE, ("seo2",)),
    ]


def document_compare_steps() -> list[BusinessPlanStep]:
    return [
        _step("d1", "extract_documents", CAP_DOC_COMPARE, STEP_READ),
        _step("d2", "compare_documents", CAP_DOC_COMPARE, STEP_ANALYZE, ("d1",)),
        _step("d3", "difference_report", CAP_DOC_COMPARE, STEP_GENERATE, ("d2",)),
    ]


def communication_steps() -> list[BusinessPlanStep]:
    return [
        _step("c1", "retrieve_email_context", CAP_EMAIL, STEP_READ),
        _step("c2", "draft_reply", CAP_CONTENT, STEP_GENERATE, ("c1",)),
        _step("c3", "preview_send", CAP_EMAIL, STEP_PREPARE_WRITE, ("c2",), requires_approval=True, risk_level="HIGH"),
    ]


def daily_report_steps() -> list[BusinessPlanStep]:
    return [
        _step("r1", "commerce_snapshot", CAP_COMMERCE, STEP_READ),
        _step("r2", "marketplace_snapshot", CAP_MARKETPLACE, STEP_READ),
        _step("r3", "seo_snapshot", CAP_SEO, STEP_READ),
        _step("r4", "compose_report", CAP_CONTENT, STEP_GENERATE, ("r1", "r2", "r3")),
    ]


def onec_price_steps(*, constraints: BusinessConstraint) -> list[BusinessPlanStep]:
    steps = [
        _step("c1", "onec_resolve_price_target", CAP_ERP_1C, STEP_READ),
        _step("c2", "onec_price_preview", CAP_ERP_1C, STEP_PREPARE_WRITE, ("c1",), requires_approval=True, risk_level="HIGH"),
    ]
    if not constraints.read_only:
        steps.append(
            _step("c3", "onec_price_apply", CAP_ERP_1C, STEP_WRITE, ("c2",), requires_approval=True, risk_level="CRITICAL")
        )
        steps.append(_step("c4", "onec_price_verify", CAP_ERP_1C, STEP_VERIFY, ("c3",)))
    return steps


def select_recipe(intent: str, text: str, constraints: BusinessConstraint) -> str:
    tl = (text or "").casefold()
    if any(w in tl for w in ("1с", "1c", "onec")) and any(w in tl for w in ("цен", "price", "стоим")):
        return RECIPE_ONEC_PRICE
    if any(w in tl for w in ("договор", "document", "pdf", "docx", "сравни два")):
        return RECIPE_DOCUMENT_COMPARE
    if any(w in tl for w in ("письм", "email", "ответ поставщик", "перед отправкой")) and "прайс" not in tl:
        return RECIPE_COMMUNICATION
    if any(w in tl for w in ("заказ", "orders")) and any(
        w in tl for w in ("ozon", "wb", "wildberries", "яндекс", "yandex", "маркетплейс", "marketplace")
    ):
        return RECIPE_MARKETPLACE_PROFIT
    if any(w in tl for w in ("seo", "семант")) and "прайс" not in tl:
        return RECIPE_SEO_REVIEW
    if any(w in tl for w in ("profitability", "убыточ", "loss", "марж")) and "прайс" not in tl and "поставщик" not in tl:
        return RECIPE_MARKETPLACE_PROFIT
    if any(w in tl for w in ("запуск", "launch", "карточк")) and "прайс" not in tl:
        return RECIPE_PRODUCT_LAUNCH
    if any(w in tl for w in ("ежедневн", "daily report")):
        return RECIPE_DAILY_REPORT
    if any(w in tl for w in ("прайс", "поставщик", "samsung", "марж", "маркетплейс", "marketplace", "bitrix", "публикац")):
        return RECIPE_SUPPLIER_PRICE
    if intent in {"PUBLISH", "PREPARE", "MULTI_STEP_BUSINESS_TASK"}:
        return RECIPE_SUPPLIER_PRICE
    return RECIPE_GENERIC


def steps_for_recipe(recipe: str, *, constraints: BusinessConstraint, publish: bool) -> list[BusinessPlanStep]:
    if recipe == RECIPE_SUPPLIER_PRICE:
        return supplier_price_steps(constraints=constraints, publish=publish)
    if recipe == RECIPE_PRODUCT_LAUNCH:
        return product_launch_steps(constraints=constraints)
    if recipe == RECIPE_MARKETPLACE_PROFIT:
        return marketplace_profit_steps()
    if recipe == RECIPE_SEO_REVIEW:
        return seo_review_steps()
    if recipe == RECIPE_DOCUMENT_COMPARE:
        return document_compare_steps()
    if recipe == RECIPE_COMMUNICATION:
        return communication_steps()
    if recipe == RECIPE_DAILY_REPORT:
        return daily_report_steps()
    if recipe == RECIPE_ONEC_PRICE:
        return onec_price_steps(constraints=constraints)
    return [
        _step("g1", "analyze_request", CAP_COMMERCE, STEP_ANALYZE),
        _step("g2", "prepare_summary", CAP_CONTENT, STEP_GENERATE, ("g1",)),
    ]
