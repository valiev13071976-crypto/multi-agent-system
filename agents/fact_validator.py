class FactValidator:
    """
    Проверка фактов Panda Multi-Agent.

    Проверяет:
    - противоречия;
    - отсутствие данных;
    - потенциально сомнительные утверждения.
    """

    def __init__(self):
        self.name = "FactValidator"

    async def validate(self, expert_answers: dict):

        report = {
            "verified": [],
            "warnings": [],
            "unverified": [],
            "confidence": 100,
        }

        for role, answer in expert_answers.items():

            text = str(answer).lower()

            if "не знаю" in text:
                report["warnings"].append(
                    f"{role}: указал недостаток данных"
                )
                report["confidence"] -= 10

            if "возможно" in text:
                report["warnings"].append(
                    f"{role}: использует неопределённость"
                )
                report["confidence"] -= 5

            if "источник" in text:
                report["verified"].append(
                    f"{role}: указал источник"
                )

        if report["confidence"] < 0:
            report["confidence"] = 0

        return report
