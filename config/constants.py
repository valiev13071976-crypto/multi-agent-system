# Panda Multi-Agent v1.0
# System constants


# Таймауты агентов (секунды)

AGENT_TIMEOUT = 5.0

FACT_VALIDATOR_TIMEOUT = 3.0

JUDGE_TIMEOUT = 5.0

PEER_REVIEW_TIMEOUT = 5.0


# Повторы при ошибках

MAX_RETRIES = 2

RETRY_BACKOFF_SECONDS = 1


# Ограничение контекста

MAX_CONTEXT_CHARS = 30000


# Минимальная уверенность

DEFAULT_CONFIDENCE = 50


# Порог проверки фактов

FACT_CHECK_THRESHOLD = 0.8


# Вес экспертов по умолчанию

DEFAULT_EXPERT_WEIGHTS = {
    "strategist": 0.25,
    "critic": 0.25,
    "researcher": 0.25,
    "technical": 0.25,
}


# Схема ответа экспертов

EXPERT_RESPONSE_FIELDS = [
    "role",
    "analysis",
    "facts",
    "risks",
    "recommendation",
    "confidence"
]


# Схема ответа Judge

JUDGE_RESPONSE_FIELDS = [
    "decision",
    "reasoning",
    "risks",
    "action_plan",
    "confidence"
]


# Поведение при отказах

REFUSAL_PATTERNS = [
    "я не могу",
    "не могу помочь",
    "это незаконно",
    "это запрещено",
    "не имею права",
    "невозможно ответить"
]


# Логи

LOG_FILES = {
    "decisions": "logs/decisions.log",
    "errors": "logs/errors.log",
    "refusals": "logs/refusals.log",
}
