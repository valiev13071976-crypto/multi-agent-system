from agents.validators.models import STATUS_UNKNOWN, PeerReviewResult
from security.redaction import redact


class PeerReview:
    """
    Deterministic metadata about expert answers.
    Not a final Judge and not a semantic quality score.
    """

    def __init__(self):
        self.name = "PeerReview"

    async def review(self, expert_answers: dict, errors=None):
        answered = tuple(sorted(str(provider_id) for provider_id in (expert_answers or {})))
        failed = tuple(sorted(str(provider_id) for provider_id in (errors or {})))
        issues = tuple(
            redact(f"missing_provider:{provider_id}") for provider_id in failed
        )
        return PeerReviewResult(
            validator_id="peer_review",
            status=STATUS_UNKNOWN,
            score=0.0,
            issues=issues,
            evidence={
                "answer_count": len(answered),
                "failed_count": len(failed),
                "answered_provider_ids": answered,
                "failed_provider_ids": failed,
            },
            reason="metadata_only",
            answered_provider_ids=answered,
            failed_provider_ids=failed,
        )
