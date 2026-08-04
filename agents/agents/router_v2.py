from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent
from agents.grok_agent import GrokAgent
from agents.deepseek_agent import DeepSeekAgent

from agents.peer_review import PeerReview
from agents.fact_validator import FactValidator
from agents.judge import Judge

from agents.core.pipeline import Pipeline
from agents.core.expert_manager import ExpertManager
from agents.core.response_formatter import ResponseFormatter
from agents.core.decision_memory import DecisionMemory
from agents.core.supervisor import Supervisor


class RouterV2:
    """
    Panda Multi-Agent V2
    """

    def __init__(self):

        expert_manager = ExpertManager(
            strategist=OpenAIAgent(),
            critic=AnthropicAgent(),
            researcher=GeminiAgent(),
            trend_agent=GrokAgent(),
            technical=DeepSeekAgent(),
        )

        self.pipeline = Pipeline(
            expert_manager=expert_manager,
            peer_review=PeerReview(),
            fact_validator=FactValidator(),
            judge=Judge(),
            response_formatter=ResponseFormatter(),
            supervisor=Supervisor(),
            decision_memory=DecisionMemory(),
        )

    async def run(
        self,
        prompt: str,
    ):
        return await self.pipeline.execute(prompt)
