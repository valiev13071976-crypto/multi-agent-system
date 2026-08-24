class Pipeline:
    """
    Главный конвейер Panda Multi-Agent V2.
    """

    def __init__(
        self,
        expert_manager,
        peer_review,
        fact_validator,
        judge,
        response_formatter,
        supervisor,
        decision_memory,
    ):
        self.expert_manager = expert_manager
        self.peer_review = peer_review
        self.fact_validator = fact_validator
        self.judge = judge
        self.response_formatter = response_formatter
        self.supervisor = supervisor
        self.decision_memory = decision_memory

    async def execute(self, prompt: str, selected=None):

        experts = await self.expert_manager.run(prompt, selected=selected)

        peer = await self.peer_review.review(experts)

        facts = await self.fact_validator.validate(
            experts,
        )

        decision = await self.judge.run(
            experts=experts,
            peer_review=peer,
            fact_report=facts,
        )

        answer = await self.response_formatter.format(
            decision
        )

        await self.decision_memory.save(
            prompt,
            answer,
        )

        return answer
