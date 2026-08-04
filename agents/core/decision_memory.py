class DecisionMemory:
    """
    Простая память решений.
    Позже будет заменена на SQLite/PostgreSQL/Vector DB.
    """

    def __init__(self):
        self.history = []

    async def save(self, prompt, answer):

        self.history.append(
            {
                "prompt": prompt,
                "answer": answer,
            }
        )

    async def last(self, limit=10):
        return self.history[-limit:]
