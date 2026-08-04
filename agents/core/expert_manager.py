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


    async def run(self, prompt: str):

        tasks = [
            self.strategist.run(prompt),
            self.critic.run(prompt),
            self.researcher.run(prompt),
            self.trend_agent.run(prompt),
            self.technical.run(prompt),
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        return {
            "strategist": str(results[0]),
            "critic": str(results[1]),
            "researcher": str(results[2]),
            "trend_agent": str(results[3]),
            "technical": str(results[4]),
        }
