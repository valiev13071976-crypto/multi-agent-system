"""Canonical product definition — claims must map to implemented capabilities only."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityClaim:
    id: str
    name: str
    family: str
    availability: str  # LIVE | READY | COMING | NOT_ACTIVATED
    summary: str
    example_task: str
    requires_approval_for_write: bool
    integrations_required: tuple[str, ...]


PRODUCT_NAME = "Panda"
PRODUCT_TAGLINE = "Управляемый бизнес-ассистент, а не просто чат"

WHAT_IS_PANDA = (
    "Panda — серверный AI-ассистент для бизнеса: документы, Excel, контент, "
    "аналитика и интеграции выполняются через единый governed-контур "
    "(identity, tenant, workflow, ToolGateway, HITL, бюджет)."
)

WHO_FOR = (
    "Предприниматели и команды, которым нужен помощник с контролем доступа, "
    "аудитом и безопасными внешними действиями — не автономный «чёрный ящик»."
)

PROBLEM = (
    "Обычный AI-чат не разделяет tenant, не знает планы/лимиты, не проводит "
    "WRITE через HITL и не связывает каналы (Web/Telegram/Voice) с одной политикой."
)

DIFFERENTIATOR = (
    "Один Panda-core для всех каналов: capability + entitlement + workflow + "
    "ToolGateway + audit. Внешний WRITE по умолчанию запрещён до явного одобрения."
)

CAPABILITIES: tuple[CapabilityClaim, ...] = (
    CapabilityClaim(
        "ai_assistant",
        "AI Assistant",
        "core",
        "READY",
        "Диалог и задачи через Business Assistant / Panda core.",
        "Сформулировать задачу и получить план/ответ в чате.",
        True,
        (),
    ),
    CapabilityClaim(
        "files_documents",
        "Files / Documents",
        "data",
        "READY",
        "Загрузка и анализ документов в governed runtime.",
        "Загрузить PDF/DOCX и задать вопрос по содержанию.",
        False,
        (),
    ),
    CapabilityClaim(
        "excel_data",
        "Excel / Data Intelligence",
        "data",
        "READY",
        "Нормализация, поиск, сверка табличных данных.",
        "Сравнить два прайса и найти расхождения.",
        False,
        (),
    ),
    CapabilityClaim(
        "content_media_seo",
        "Content / Media / SEO",
        "growth",
        "READY",
        "Контент и медиа-подготовка, SEO-анализ (engineering-ready).",
        "Подготовить черновик карточки товара и SEO-заметки.",
        True,
        (),
    ),
    CapabilityClaim(
        "marketplace",
        "Marketplace workflows",
        "commerce",
        "NOT_ACTIVATED",
        "Адаптеры WB/Ozon/YM engineering-ready; LIVE READ не активирован.",
        "Прочитать остатки продавца (после LIVE activation).",
        True,
        ("wildberries", "ozon", "yandex_market"),
    ),
    CapabilityClaim(
        "erp_cms",
        "1C / Bitrix",
        "commerce",
        "NOT_ACTIVATED",
        "Engineering closed; operational activation out of this block.",
        "Прочитать номенклатуру 1C (отдельная активация).",
        True,
        ("onec", "bitrix"),
    ),
    CapabilityClaim(
        "analytics",
        "Business Analytics",
        "ops",
        "READY",
        "Analytics dashboard и FinOps attribution.",
        "Посмотреть обзор метрик tenant за 30 дней.",
        False,
        (),
    ),
    CapabilityClaim(
        "telegram",
        "Telegram",
        "channel",
        "READY",
        "Telegram → тот же BA/Panda core; live bot требует human approval.",
        "Написать боту и получить ответ Panda.",
        True,
        ("telegram",),
    ),
    CapabilityClaim(
        "voice",
        "Voice",
        "channel",
        "READY",
        "STT → Panda → optional TTS; внешние провайдеры только после approval.",
        "Отправить голосовое и получить текстовый/голосовой ответ.",
        True,
        ("speech",),
    ),
    CapabilityClaim(
        "scheduled_automation",
        "Scheduled Automation",
        "automation",
        "READY",
        "Durable schedules с tenant/budget/HITL границами.",
        "Создать ежедневный безопасный internal workflow.",
        True,
        (),
    ),
    CapabilityClaim(
        "controlled_automation",
        "Controlled Automation",
        "automation",
        "READY",
        "Условные действия с risk class и fail-closed policy.",
        "Триггер → policy → HITL → audit.",
        True,
        (),
    ),
    CapabilityClaim(
        "accounts_billing",
        "Accounts / Plans / Billing foundation",
        "saas",
        "READY",
        "Login/session, trial/paid/complimentary, entitlements; acquiring inactive.",
        "Войти и увидеть план/лимиты.",
        False,
        (),
    ),
)


def product_definition() -> dict:
    return {
        "name": PRODUCT_NAME,
        "tagline": PRODUCT_TAGLINE,
        "what_is": WHAT_IS_PANDA,
        "who_for": WHO_FOR,
        "problem": PROBLEM,
        "differentiator": DIFFERENTIATOR,
        "capabilities": [
            {
                "id": c.id,
                "name": c.name,
                "family": c.family,
                "availability": c.availability,
                "summary": c.summary,
                "example_task": c.example_task,
                "requires_approval_for_write": c.requires_approval_for_write,
                "integrations_required": list(c.integrations_required),
            }
            for c in CAPABILITIES
        ],
        "availability_legend": {
            "LIVE": "Operationally verified with real external evidence",
            "READY": "Engineering/offline ready; live may need human approval",
            "COMING": "Planned, not claimed as available",
            "NOT_ACTIVATED": "Built but not operationally activated",
        },
        "unsupported_marketing_claims": [
            "best AI",
            "revolutionary AI",
            "fully legally compliant",
            "all marketplaces live",
        ],
    }
