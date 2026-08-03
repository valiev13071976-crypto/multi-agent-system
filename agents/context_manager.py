import logging


class ContextManager:
    """
    Управление входным контекстом для Panda Multi-Agent.

    Задачи:
    - очистка входных данных
    - удаление дублей
    - ограничение размера
    - подготовка контекста для экспертов
    """

    def __init__(self, max_chars: int = 30000):
        self.max_chars = max_chars

    async def prepare(self, user_request: str, extra_context=None):
        """
        Подготавливает контекст перед отправкой агентам.
        """

        context = {
            "user_request": user_request,
            "additional_context": extra_context or {},
            "cleaned": True
        }

        context = self.remove_empty(context)
        context = self.limit_size(context)

        logging.info("Context prepared successfully")

        return context

    def remove_empty(self, data):
        """
        Удаляет пустые значения.
        """

        if isinstance(data, dict):
            return {
                key: self.remove_empty(value)
                for key, value in data.items()
                if value not in [None, "", [], {}]
            }

        if isinstance(data, list):
            return [
                item for item in data
                if item not in [None, "", {}, []]
            ]

        return data

    def limit_size(self, context):
        """
        Ограничение размера контекста v1.
        """

        text = str(context)

        if len(text) > self.max_chars:
            text = text[:self.max_chars]

        return {
            "context": text
        }

    def get_for_role(self, context, role: str):
        """
        Простая маршрутизация контекста для ролей.
        """

        if role == "researcher":
            return {
                "request": context.get("user_request"),
                "focus": "facts, prices, market data"
            }

        if role == "technical":
            return {
                "request": context.get("user_request"),
                "focus": "architecture, implementation"
            }

        if role == "critic":
            return {
                "request": context.get("user_request"),
                "focus": "risks and weak points"
            }

        if role == "strategist":
            return {
                "request": context.get("user_request"),
                "focus": "strategy and decisions"
            }

        return context
