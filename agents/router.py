import asyncio

from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent
from agents.grok_agent import GrokAgent
from agents.deepseek_agent import DeepSeekAgent


class Router:
    """
    Главный оркестратор Panda Multi-Agent v1.0

    Поток:
    Context
    ↓
    Эксперты
    ↓
    Peer Review (позже)
    ↓
    Fact Validator (позже)
    ↓
    Judge (позже)
    """

    def __init__(self):

        # временно используем модели как экспертов
        # позже заменим на отдельные роли

        self.strategist = OpenAIAgent()
        self.critic = AnthropicAgent()
        self.researcher = GeminiAgent()
        self.trend_agent = GrokAgent()
        self.technical = DeepSeekAgent()


    async def run(
        self,
        prompt: str,
        mode: str = "both"
    ):

        expert_tasks = [
            self.strategist.run(
                f"""
Ты Стратег.

Задача:

{prompt}

Дай:
- стратегию;
- варианты;
- расчёты;
- риски.
"""
            ),

            self.critic.run(
                f"""
Ты Критик.

Задача:

{prompt}

Найди:
- слабые места;
- риски;
- ошибки предположений.
"""
            ),

            self.researcher.run(
                f"""
Ты Исследователь.

Задача:

{prompt}

Дай:
- факты;
- данные;
- источники;
- проверки.
"""
            ),

            self.technical.run(
                f"""
Ты Технический эксперт.

Задача:

{prompt}

Дай:
- техническое решение;
- ограничения;
- архитектуру.
"""
            ),
        ]


        results = await asyncio.gather(
            *expert_tasks,
            return_exceptions=True
        )


        return {
            "model": "multi-agent",
            "strategist": str(results[0]),
            "critic": str(results[1]),
            "researcher": str(results[2]),
            "technical": str(results[3]),
        }
