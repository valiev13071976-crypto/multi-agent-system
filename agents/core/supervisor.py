class Supervisor:
    """
    Главный управляющий Panda V2.

    Решает:
    - сколько экспертов запускать;
    - нужен ли интернет;
    - нужен ли Judge;
    - нужен ли Fact Validator.
    """

    def __init__(self):
        pass

    async def decide(self, prompt: str):

        text = prompt.lower()

        decision = {
            "experts": [
                "strategist"
            ],

            "peer_review": False,
            "fact_validation": False,
            "use_memory": True,
            "use_web": False,
            "response_style": "short",
        }

        if any(
            word in text
            for word in [
                "сравни",
                "проанализируй",
                "анализ",
                "рынок",
                "закуп",
                "бизнес",
            ]
        ):

            decision["experts"] = [
                "strategist",
                "critic",
                "researcher",
                "technical",
            ]

            decision["peer_review"] = True

        if any(
            word in text
            for word in [
                "сегодня",
                "новости",
                "курс",
                "цена",
                "сейчас",
                "последние",
            ]
        ):

            decision["use_web"] = True
            decision["fact_validation"] = True

        return decision
