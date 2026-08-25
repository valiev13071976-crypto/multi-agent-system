from agents.validators.models import (
    FACT_HEAVY_CATEGORIES,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    ConfidenceInputs,
)

CONFIDENCE_MIN = 0.05
CONFIDENCE_MAX = 0.95
BASE_CONFIDENCE = 0.50


def compute_confidence(inputs: ConfidenceInputs) -> float:
    score = BASE_CONFIDENCE
    if inputs.successful_experts >= 2:
        score += 0.10
    if inputs.structural_all_pass and inputs.successful_experts >= 1:
        score += 0.10
    if inputs.consistency_status == STATUS_PASS:
        score += 0.10
    if inputs.sources_present:
        score += 0.05
    if inputs.factual_status == STATUS_PASS:
        score += 0.10
    if inputs.structural_fail or inputs.successful_experts == 0:
        score -= 0.20
    if inputs.consistency_status == STATUS_FAIL:
        score -= 0.15
    if inputs.failed_providers >= 1:
        score -= 0.10
    if inputs.factual_status == STATUS_FAIL:
        score -= 0.20
    elif (
        inputs.factual_status in (STATUS_UNKNOWN, STATUS_WARN)
        and (inputs.category or "") in FACT_HEAVY_CATEGORIES
    ):
        score -= 0.10
    if score < CONFIDENCE_MIN:
        return CONFIDENCE_MIN
    if score > CONFIDENCE_MAX:
        return CONFIDENCE_MAX
    return round(score, 2)
