import re

from agents.validators.models import (
    STATUS_FAIL,
    STATUS_PASS,
    ValidationResult,
)


VALIDATOR_ID = "structural"

PLACEHOLDER_RE = re.compile(
    r"^(?:n/?a|none|null|todo|tbd|placeholder|\.{3,}|…|\[(?:empty|placeholder)\])$",
    re.I,
)
ERROR_ONLY_RE = re.compile(
    r"^(?:traceback \(most recent call last\).*"
    r"|[A-Za-z_]*Error(?::.*)?"
    r"|[A-Za-z_]*Exception(?::.*)?)$",
    re.I | re.S,
)


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None


class StructuralValidator:
    def validate(self, answer, *, provider_id: str = "") -> ValidationResult:
        text = _as_text(answer)
        if text is None:
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_FAIL,
                score=0.0,
                issues=("unparseable_answer",),
                evidence={"provider_id": provider_id, "char_count": 0},
                reason="unparseable_answer",
            )

        stripped = text.strip()
        evidence = {
            "provider_id": provider_id,
            "char_count": len(stripped),
        }
        if not stripped:
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_FAIL,
                score=0.0,
                issues=("empty_answer",),
                evidence=evidence,
                reason="empty_answer",
            )
        if PLACEHOLDER_RE.fullmatch(stripped):
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_FAIL,
                score=0.0,
                issues=("placeholder_answer",),
                evidence=evidence,
                reason="placeholder_answer",
            )
        if ERROR_ONLY_RE.fullmatch(stripped):
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_FAIL,
                score=0.0,
                issues=("error_only_answer",),
                evidence=evidence,
                reason="error_only_answer",
            )
        if not any(ch.isalnum() for ch in stripped):
            return ValidationResult(
                validator_id=VALIDATOR_ID,
                status=STATUS_FAIL,
                score=0.0,
                issues=("no_meaningful_text",),
                evidence=evidence,
                reason="no_meaningful_text",
            )
        return ValidationResult(
            validator_id=VALIDATOR_ID,
            status=STATUS_PASS,
            score=1.0,
            issues=(),
            evidence=evidence,
            reason="meaningful_text",
        )

    def validate_experts(self, experts: dict) -> dict[str, ValidationResult]:
        results = {}
        for provider_id, answer in (experts or {}).items():
            results[str(provider_id)] = self.validate(
                answer,
                provider_id=str(provider_id),
            )
        return results
