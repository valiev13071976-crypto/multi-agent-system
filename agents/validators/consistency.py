from difflib import SequenceMatcher
import re

from agents.validators.models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    ValidationResult,
)


VALIDATOR_ID = "consistency"
VALIDATOR_VERSION = "1.0.0"

NEAR_EXACT_RATIO = 0.95

DIRECT_CONTRADICTION_PAIRS = (
    ("CONSENSUS: YES", "CONSENSUS: NO"),
    ("ИТОГ: ДА", "ИТОГ: НЕТ"),
)

WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return WHITESPACE_RE.sub(" ", str(text).strip()).casefold()


def _comparable(experts: dict) -> dict[str, str]:
    comparable = {}
    for provider_id, answer in (experts or {}).items():
        normalized = _normalize(answer or "")
        if normalized:
            comparable[str(provider_id)] = normalized
    return comparable


def _has_direct_contradiction(texts: tuple[str, ...]) -> bool:
    for left, right in DIRECT_CONTRADICTION_PAIRS:
        left_n = left.casefold()
        right_n = right.casefold()
        has_left = any(left_n in text for text in texts)
        has_right = any(right_n in text for text in texts)
        if has_left and has_right:
            return True
    return False


class ConsistencyValidator:
    def validate(self, experts: dict) -> ValidationResult:
        comparable = _comparable(experts)
        provider_ids = tuple(sorted(comparable))
        texts = tuple(comparable[pid] for pid in provider_ids)
        evidence = {
            "compared_count": len(texts),
            "provider_ids": provider_ids,
        }

        if len(texts) == 0:
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_UNKNOWN,
                score=0.0,
                issues=("no_comparable_answers",),
                evidence=evidence,
                reason="no_comparable_answers",
            )
        if len(texts) == 1:
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_UNKNOWN,
                score=0.0,
                issues=("insufficient_answers",),
                evidence=evidence,
                reason="insufficient_answers",
            )
        if len(set(texts)) == 1:
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_PASS,
                score=1.0,
                issues=(),
                evidence=evidence,
                reason="exact_agreement",
            )

        ratios = []
        unique = list(set(texts))
        for index, left in enumerate(unique):
            for right in unique[index + 1 :]:
                ratios.append(SequenceMatcher(None, left, right).ratio())
        max_ratio = max(ratios) if ratios else 0.0
        evidence = {**evidence, "max_similarity": round(max_ratio, 4)}

        if max_ratio >= NEAR_EXACT_RATIO:
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_PASS,
                score=round(max_ratio, 4),
                issues=(),
                evidence=evidence,
                reason="near_exact_agreement",
            )
        if _has_direct_contradiction(texts):
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_FAIL,
                score=0.0,
                issues=("direct_contradiction",),
                evidence=evidence,
                reason="direct_contradiction",
            )
        return ValidationResult(
            validator_id=VALIDATOR_ID,
            status=STATUS_UNKNOWN,
            score=0.0,
            issues=("no_reliable_signal",),
            evidence=evidence,
            reason="no_reliable_signal",
        )
