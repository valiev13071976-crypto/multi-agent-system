import asyncio

from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent
from agents.grok_agent import GrokAgent
from agents.deepseek_agent import DeepSeekAgent
from agents.judge import Judge


class Router:
    """
    Главный оркестратор Panda Multi-Agent v1.0

    Поток:
    Context
    ↓
    Эксперты
    ↓
    Judge
    ↓
    Итоговое решение
    """

    def __init__(self):

        self.strategist = OpenAIAgent()
        self.critic = AnthropicAgent()
        self.researcher = GeminiAgent()
        self.trend_agent = GrokAgent()
        self.technical = DeepSeekAgent()
        self.judge = Judge()


    async def run(
        self,
        prompt: str,
        mode: str = "both"
    ):

        expert_tasks = [
            self.strategist.run(
                f"""
Ты Стратег Panda Multi-Agent.

Задача:

{prompt}

Дай:
- стратегию;
- варианты решения;
- расчёты;
- риски.
"""
            ),

            self.critic.run(
                f"""
Ты Критик Panda Multi-Agent.

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
Ты Исследователь Panda Multi-Agent.

Задача:

{prompt}

Дай:
- факты;
- данные;
- источники;
- проверки.
"""
            ),

            self.trend_agent.run(
                f"""
Ты Аналитик трендов Panda Multi-Agent.

Задача:

{prompt}

Дай:
- актуальные тренды;
- изменения рынка;
- новые возможности.
"""
            ),

            self.technical.run(
                f"""
Ты Технический эксперт Panda Multi-Agent.

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


        judge_prompt = f"""
Ты главный судья Panda Multi-Agent.

Проанализируй ответы всех экспертов.

СТРАТЕГ:
{results[0]}

КРИТИК:
{results[1]}

ИССЛЕДОВАТЕЛЬ:
{results[2]}

АНАЛИТИК ТРЕНДОВ:
{results[3]}

ТЕХНИЧЕСКИЙ ЭКСПЕРТ:
{results[4]}


Сделай итог:

1. Лучшее решение.
2. Аргументы.
3. Риски.
4. Конкретный план действий.
"""


        judge_result = await self.judge.run(judge_prompt)


        return {
            "model": "multi-agent",
            "strategist": str(results[0]),
            "critic": str(results[1]),
            "researcher": str(results[2]),
            "trend_agent": str(results[3]),
            "technical": str(results[4]),
            "judge": str(judge_result),
        }
