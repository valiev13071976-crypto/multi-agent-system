import asyncio

from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent
from agents.grok_agent import GrokAgent
from agents.deepseek_agent import DeepSeekAgent


SYSTEM_RULES = """
Ты работаешь как часть мультиагентной аналитической системы Panda Multi-Agent.

Правила:
- анализируй задачу глубоко;
- используй факты и проверенные данные;
- не давай поверхностных ответов;
- предлагай практическое решение;
- учитывай бизнес, риски, стоимость и эффективность.

Роли:
OpenAI — стратегический анализ и итоговые решения.
Anthropic — анализ документов, логика и риски.
Gemini — поиск информации и сравнение данных.
Grok — тренды и актуальная информация.
DeepSeek — технические решения и автоматизация.
"""


class Router:
    def __init__(self):
        self.openai = OpenAIAgent()
        self.anthropic = AnthropicAgent()
        self.gemini = GeminiAgent()
        self.grok = GrokAgent()
        self.deepseek = DeepSeekAgent()


    async def run(self, prompt: str, mode: str = "both"):

        final_prompt = f"""
{SYSTEM_RULES}

Задача пользователя:

{prompt}
"""


        if mode == "openai":
            return {
                "model": "openai",
                "openai": await self.openai.run(final_prompt),
            }


        if mode == "anthropic":
            return {
                "model": "anthropic",
                "anthropic": await self.anthropic.run(final_prompt),
            }


        if mode == "gemini":
            return {
                "model": "gemini",
                "gemini": await self.gemini.run(final_prompt),
            }


        if mode == "grok":
            return {
                "model": "grok",
                "grok": await self.grok.run(final_prompt),
            }


        if mode == "deepseek":
            return {
                "model": "deepseek",
                "deepseek": await self.deepseek.run(final_prompt),
            }


        results = await asyncio.gather(
            self.openai.run(final_prompt),
            self.anthropic.run(final_prompt),
            self.gemini.run(final_prompt),
            self.grok.run(final_prompt),
            self.deepseek.run(final_prompt),
            return_exceptions=True,
        )


        return {
            "model": "both",
            "openai": str(results[0]),
            "anthropic": str(results[1]),
            "gemini": str(results[2]),
            "grok": str(results[3]),
            "deepseek": str(results[4]),
        }
