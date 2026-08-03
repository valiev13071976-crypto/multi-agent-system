from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent


class Router:
    def __init__(self):
        self.openai = OpenAIAgent()
        self.anthropic = AnthropicAgent()

    async def run(self, prompt: str, mode: str = "both"):
        if mode == "openai":
            return {
                "model": "openai",
                "openai": await self.openai.run(prompt),
            }

        if mode == "anthropic":
            return {
                "model": "anthropic",
                "anthropic": await self.anthropic.run(prompt),
            }

        openai_result = await self.openai.run(prompt)
        anthropic_result = await self.anthropic.run(prompt)

        return {
            "model": "both",
            "openai": openai_result,
            "anthropic": anthropic_result,
        }
