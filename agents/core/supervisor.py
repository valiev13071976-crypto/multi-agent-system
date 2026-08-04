class Supervisor:
    """
    Главный координатор Panda Multi-Agent.
    Решает, какие компоненты необходимо использовать.
    """

    def __init__(self):
        pass

    async def decide(self, prompt: str):

        prompt_lower = prompt.lower()

        return {
            "use_experts": True,
            "use_peer_review": True,
            "use_fact_validation": True,
            "use_memory": True,
            "use_web": any(
                word in prompt_lower
                for word in [
                    "сегодня",
                    "новости",
                    "цена",
                    "курс",
                    "акции",
                    "рынок",
                    "курс валют",
                    "сейчас",
                    "последние",
                ]
            ),
            "response_style": "short",
        }
