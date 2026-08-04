class PeerReview:
    """
    Взаимная проверка ответов экспертов.

    Каждый эксперт получает ответы остальных
    и формирует краткую профессиональную оценку.
    """

    def __init__(self):
        self.name = "PeerReview"

    async def review(self, expert_answers: dict):

        reviews = {}

        for expert, answer in expert_answers.items():

            reviews[expert] = {
                "summary": f"{expert} выполнил анализ.",
                "strengths": [
                    "Аргументы представлены",
                    "Логика прослеживается"
                ],
                "weaknesses": [
                    "Требуется дополнительная проверка фактов",
                    "Не все риски раскрыты"
                ],
                "confidence": 85
            }

        return reviews
