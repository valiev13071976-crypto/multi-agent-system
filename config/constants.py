# Panda Multi-Agent v1.0
# Global configuration

# Таймауты агентов (секунды)
AGENT_TIMEOUT = 5

FACT_VALIDATOR_TIMEOUT = 5

JUDGE_TIMEOUT = 10

PEER_REVIEW_TIMEOUT = 5


# Повторы при ошибках
MAX_RETRIES = 2

RETRY_BACKOFF_SECONDS = 1


# Ограничение контекста
MAX_CONTEXT_TOKENS = 8000


# Уверенность экспертов
MIN_CONFIDENCE = 0.0
MAX_CONFIDENCE = 1.0


# Дефолтные веса экспертов
DEFAULT_EXPERT_WEIGHTS = {
    "strategist": 0.25,
    "critic": 0.25,
    "researcher": 0.25,
    "technical": 0.25,
}


# Ключевые слова для определения типа задачи
TASK_KEYWORDS = {
    "business": [
        "бизнес",
        "прибыль",
        "стратегия",
        "продажи",
        "закупка"
    ],
    "technical": [
        "код",
        "api",
        "архитектура",
        "сервер",
        "программа"
    ],
    "research": [
        "цена",
        "рынок",
        "новости",
        "данные"
    ],
}


# Статусы ответов агентов
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_REFUSED = "refused"
