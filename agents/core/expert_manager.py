import asyncio


PROVIDER_IDS = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
)


class ExpertManager:
    """
    Запускает выбранных providers одновременно.
    """

    def __init__(
        self,
        openai=None,
        anthropic=None,
        gemini=None,
        grok=None,
        deepseek=None,
    ):
        self.openai = openai
        self.anthropic = anthropic
        self.gemini = gemini
        self.grok = grok
        self.deepseek = deepseek
        self.last_errors = {}

    def get_provider(self, provider_id: str):
        if provider_id not in PROVIDER_IDS:
            return None
        return getattr(self, provider_id)

    async def run(self, prompt: str, selected=None):

        if selected is None:
            available = [
                (provider_id, self.get_provider(provider_id))
                for provider_id in PROVIDER_IDS
                if self.get_provider(provider_id) is not None
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

        for (provider_id, _), result in zip(available, results):
            if isinstance(result, BaseException):
                self.last_errors[provider_id] = {
                    "type": type(result).__name__,
                    "message": str(result),
                }
                continue

            experts[provider_id] = str(result)

        return experts
