from agents.validators.confidence import compute_confidence
from agents.validators.models import (
    STATUS_FAIL,
    STATUS_PASS,
    ConfidenceInputs,
    ValidationResult,
)
from security.redaction import redact


JUDGE_VERSION = "1.0.0"


class Judge:
    """
    Deterministic aggregator of expert answers and validator results.
    """

    judge_version = JUDGE_VERSION

    def __init__(self, observability=None):
        self.name = "Judge"
        self.observability = observability
        self.judge_version = JUDGE_VERSION

    def _legacy_run(self, prompt: str) -> dict:
        confidence = 90
        text = str(prompt)
        if "Exception" in text or "Traceback" in text:
            confidence -= 40
        if "ошибка" in text.lower():
            confidence -= 20
        confidence = max(confidence, 0)
        return {
            "role": self.name,
            "summary": "Финальный анализ успешно сформирован.",
            "best_solution": (
                "Использовать решение, подтвержденное большинством "
                "экспертов и проверкой фактов."
            ),
            "final_answer": "",
            "experts": {},
            "confidence": confidence,
            "analysis": text,
            "risks": [
                "Проверить исходные данные",
                "Проверить фактические источники",
                "Проверить ограничения выбранного решения",
            ],
            "action_plan": [
                "Выбрать оптимальное решение",
                "Проверить реализацию",
                "Провести тестирование",
                "Измерить результат",
                "Скорректировать стратегию при необходимости",
            ],
        }

    def _analysis(self, experts: dict) -> str:
        if not experts:
            return "Нет успешных ответов экспертов."
        lines = []
        for provider_id in sorted(experts):
            lines.append(f"{provider_id}: {experts[provider_id]}")
        return "\n".join(lines)

    def _expert_text(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        text = getattr(value, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        return ""

    def _serialized_experts(self, experts: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for provider_id in sorted(experts or {}):
            text = self._expert_text(experts[provider_id])
            if text:
                out[str(provider_id)] = text
        return out

    def _user_facing_from_experts(self, experts: dict) -> str:
        """Existing aggregation: all successful expert texts, sorted provider id, no labels."""
        parts = [self._expert_text(experts[pid]) for pid in sorted(experts or {})]
        return "\n".join(part for part in parts if part).strip()

    def _risks(self, fact, consistency, structural_fail: bool, failed_count: int) -> list:
        risks = [
            "Проверить исходные данные",
            "Проверить ограничения выбранного решения",
        ]
        if fact is not None and fact.status == STATUS_FAIL:
            risks.append("Внешние источники противоречат утверждениям")
        elif fact is not None and fact.reason in (
            "no_external_evidence",
            "insufficient_evidence",
            "external_evidence_timeout",
            "external_evidence_unavailable",
            "low_trust_only",
            "no_extractable_claims",
        ):
            risks.append("Факты не подтверждены внешними источниками")
        if consistency is not None and consistency.status == STATUS_FAIL:
            risks.append("Ответы экспертов содержат прямое противоречие")
        if structural_fail:
            risks.append("Часть ответов структурно непригодна")
        if failed_count:
            risks.append("Часть providers не вернула ответ")
        return risks

    async def run(
        self,
        experts=None,
        peer_review=None,
        fact_report=None,
        prompt=None,
        structural=None,
        consistency=None,
        provider_errors=None,
        category=None,
    ):
        if prompt is not None and experts is None:
            return self._legacy_run(prompt)
        if isinstance(experts, str) and structural is None:
            return self._legacy_run(experts)

        experts = experts or {}
        provider_errors = provider_errors or {}
        structural = structural or {}
        structural_values = tuple(structural.values())
        structural_fail = any(
            result.status == STATUS_FAIL for result in structural_values
        )
        structural_all_pass = bool(structural_values) and all(
            result.status == STATUS_PASS for result in structural_values
        )
        sources_present = False
        factual_status = "unknown"
        if isinstance(fact_report, ValidationResult):
            sources_present = bool(fact_report.evidence.get("sources_present"))
            factual_status = fact_report.status
        consistency_status = (
            consistency.status if isinstance(consistency, ValidationResult) else STATUS_FAIL
        )
        if consistency is None:
            consistency_status = "unknown"

        confidence = compute_confidence(
            ConfidenceInputs(
                successful_experts=len(experts),
                failed_providers=len(provider_errors),
                structural_fail=structural_fail,
                structural_all_pass=structural_all_pass,
                consistency_status=consistency_status,
                sources_present=sources_present,
                factual_status=factual_status,
                category=category,
            )
        )
        payload = {
            "role": self.name,
            "summary": "Финальный анализ успешно сформирован.",
            "best_solution": (
                "Синтез ответов экспертов без скрытого приоритета provider. "
                "Внешняя проверка фактов учитывается только при независимых источниках."
            ),
            "final_answer": self._user_facing_from_experts(experts),
            "experts": self._serialized_experts(experts),
            "confidence": confidence,
            "analysis": self._analysis(experts),
            "risks": self._risks(
                fact_report if isinstance(fact_report, ValidationResult) else None,
                consistency if isinstance(consistency, ValidationResult) else None,
                structural_fail,
                len(provider_errors),
            ),
            "action_plan": [
                "Выбрать оптимальное решение",
                "Проверить реализацию",
                "Провести тестирование",
                "Измерить результат",
                "Скорректировать стратегию при необходимости",
            ],
            "validation": {
                "structural_fail": structural_fail,
                "consistency_reason": (
                    redact(consistency.reason)
                    if isinstance(consistency, ValidationResult)
                    else ""
                ),
                "fact_reason": (
                    redact(fact_report.reason)
                    if isinstance(fact_report, ValidationResult)
                    else ""
                ),
                "peer_reason": redact(getattr(peer_review, "reason", "") or ""),
            },
        }
        if self.observability is not None:
            from observability.helpers import safe_emit

            safe_emit(
                self.observability,
                "judge.completed",
                context=self.observability.create_context(),
                component="judge",
                status="completed",
                metadata={
                    "validator_type": "judge",
                    "pass": not structural_fail,
                    "confidence": confidence,
                    "reason_code_count": len(payload.get("risks") or ()),
                },
            )
        return payload
