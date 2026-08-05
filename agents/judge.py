class Judge:
    """
    Финальный оценщик Panda Multi-Agent.
    """

    def __init__(self):
        self.name = "Judge"

    async def run(
        self,
        experts=None,
        peer_review=None,
        fact_report=None,
        prompt=None,
    ):

        # Совместимость со старой V1
        if prompt is None:
            prompt = ""

            if experts:
                prompt += f"ЭКСПЕРТЫ:\n{experts}\n\n"

            if peer_review:
                prompt += f"PEER REVIEW:\n{peer_review}\n\n"

            if fact_report:
                prompt += f"FACT REPORT:\n{fact_report}\n"

        confidence = 90

        text = str(prompt)

        if "Exception" in text or "Traceback" in text:
            confidence -= 40

        if "ошибка" in text.lower():
            confidence -= 20

        confidence = max(confidence, 0)

        return {
            "role": self.name,

            "summary": "Финальный анализ успешно сформирован.",

            "best_solution": (
                "Использовать решение, подтвержденное большинством "
                "экспертов и проверкой фактов."
            ),

            "confidence": confidence,

            "analysis": text,

            "risks": [
                "Проверить исходные данные",
                "Проверить фактические источники",
                "Проверить ограничения выбранного решения",
            ],

            "action_plan": [
                "Выбрать оптимальное решение",
                "Проверить реализацию",
                "Провести тестирование",
                "Измерить результат",
                "Скорректировать стратегию при необходимости",
            ],
        }
