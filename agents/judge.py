class Judge:
    """
    Финальный оценщик Panda Multi-Agent.

    Получает ответы экспертов и
    формирует итоговое решение.
    """

    def __init__(self):
        self.name = "Judge"

    async def run(self, prompt: str):

        confidence = 90

        if "Exception" in prompt or "Traceback" in prompt:
            confidence -= 40

        if "ошибка" in prompt.lower():
            confidence -= 20

        if confidence < 0:
            confidence = 0

        return {
            "role": self.name,

            "summary": "Финальный анализ успешно сформирован.",

            "best_solution": (
                "Использовать решение, которое подтверждается "
                "большинством экспертов и имеет минимальные риски."
            ),

            "confidence": confidence,

            "analysis": prompt,

            "risks": [
                "Проверить исходные данные",
                "Проверить фактические источники",
                "Проверить ограничения выбранного решения"
            ],

            "action_plan": [
                "Выбрать оптимальное решение",
                "Проверить реализацию",
                "Провести тестирование",
                "Измерить результат",
                "Скорректировать стратегию при необходимости"
            ]
        }
