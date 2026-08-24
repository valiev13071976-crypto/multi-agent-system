import asyncio


class ExpertManager:
    """
    Запускает всех экспертов одновременно.
    """

    def __init__(
        self,
        strategist,
        critic,
        researcher,
        trend_agent,
        technical,
    ):
        self.strategist = strategist
        self.critic = critic
        self.researcher = researcher
        self.trend_agent = trend_agent
        self.technical = technical
        self.last_errors = {}

    async def run(self, prompt: str, selected=None):

        if selected is None:
            roles = [
                ("strategist", self.strategist),
                ("critic", self.critic),
                ("researcher", self.researcher),
                ("trend_agent", self.trend_agent),
                ("technical", self.technical),
            ]
            available = [
                (role, agent)
                for role, agent in roles
                if agent is not None
            ]
        else:
            available = list(selected)

        self.last_errors = {}

        if not available:
            return {}

        results = await asyncio.gather(
            *[agent.run(prompt) for _, agent in available],
            return_exceptions=True,
        )

        experts = {}

        for (role, _), result in zip(available, results):
            if isinstance(result, BaseException):
                self.last_errors[role] = {
                    "type": type(result).__name__,
                    "message": str(result),
                }
                continue

            experts[role] = str(result)

        return experts
