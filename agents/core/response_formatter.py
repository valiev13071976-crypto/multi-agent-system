class ResponseFormatter:
    """
    Формирует компактный ответ Panda V2.
    """

    async def format(self, decision):

        if not isinstance(decision, dict):
            return {
                "summary": str(decision),
                "analysis": "",
                "final_answer": "",
                "experts": {},
                "risks": [],
                "action_plan": [],
                "confidence": 0,
            }

        return {
            "summary": decision.get(
                "summary",
                decision.get("decision", "Готово.")
            ),

            "best_solution": decision.get(
                "best_solution",
                ""
            ),

            "final_answer": decision.get("final_answer", "") or "",

            "experts": dict(decision.get("experts") or {}),

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
                0
            ),

            "role": decision.get(
                "role",
                "Judge"
            ),
        }
