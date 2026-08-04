class ResponseFormatter:
    """
    Формирует короткий и удобный ответ пользователю.
    """

    async def format(self, decision):

        if isinstance(decision, dict):

            return {
                "summary": decision.get(
                    "decision",
                    "Готово."
                ),

                "analysis": decision.get(
                    "analysis",
                    ""
                ),

                "risks": decision.get(
                    "risks",
                    []
                ),

                "action_plan": decision.get(
                    "action_plan",
                    []
                ),

                "confidence": decision.get(
                    "confidence",
                    "Средняя"
                )
            }

        return {
            "summary": str(decision),
            "analysis": "",
            "risks": [],
            "action_plan": [],
            "confidence": "Неизвестно"
        }
