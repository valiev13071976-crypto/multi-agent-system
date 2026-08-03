from datetime import datetime


class ContextManager:
    """
    Подготовка контекста для мультиагентной системы.

    v1.0:
    - очистка входных данных;
    - удаление дублей;
    - ограничение размера;
    - подготовка структуры для экспертов.
    """

    def __init__(self, max_tokens=8000):
        self.max_tokens = max_tokens


    async def prepare(self, user_prompt: str, extra_context=None):

        context = {
            "task": user_prompt,
            "additional_data": extra_context or {},
            "created_at": datetime.utcnow().isoformat(),
        }

        context = self.clean(context)

        return context


    def clean(self, context):

        cleaned = {}

        for key, value in context.items():

            if value is None:
                continue

            if isinstance(value, str):
                value = value.strip()

                if not value:
                    continue

            cleaned[key] = value


        return cleaned


    def build_expert_context(self, context, role):

        """
        Подготовка контекста под конкретного эксперта.
        """

        return {
            "role": role,
            "context": context
        }
