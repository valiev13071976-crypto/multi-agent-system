import asyncio

from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent


class Router:
    def __init__(self):
        self.openai = OpenAIAgent()
        self.anthropic = AnthropicAgent()
        self.gemini = GeminiAgent()

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

if mode == "gemini":
    return {
        "model": "gemini",
        "gemini": await self.gemini.run(prompt),
    }

openai_result, anthropic_result, gemini_result = await asyncio.gather(
    self.openai.run(prompt),
    self.anthropic.run(prompt),
    self.gemini.run(prompt),
)


return {
    "model": "both",
    "openai": openai_result,
    "anthropic": anthropic_result,
    "gemini": gemini_result,
}
