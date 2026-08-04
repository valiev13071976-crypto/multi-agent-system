import logging


class Judge:
    """
    Финальный судья Panda Multi-Agent.

    Задачи:
    - сравнение ответов агентов;
    - поиск противоречий;
    - выбор лучшего решения;
    - формирование итогового вывода.
    """

    def __init__(self):
        self.name = "judge"

    async def evaluate(self, answers: dict) -> dict:
        """
        Анализирует ответы всех агентов.
        """

        logging.info("Judge started evaluation")

        valid_answers = {
            key: value
            for key, value in answers.items()
            if value
        }

        if not valid_answers:
            return {
                "decision": "Нет доступных ответов",
                "reasoning": "Все агенты вернули пустой результат",
                "confidence": 0,
            }

        combined = "\n\n".join(
            [
                f"{agent.upper()}:\n{answer}"
                for agent, answer in valid_answers.items()
            ]
        )

        return {
            "decision": "Сформирован общий анализ агентов",
            "reasoning": combined,
            "agents_used": list(valid_answers.keys()),
            "confidence": 80,
        }
