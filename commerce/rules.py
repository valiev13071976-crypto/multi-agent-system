"""Versioned compliance rules engine — deterministic, not prompt-based."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from commerce.capabilities import (
    CAP_EDO_SEND,
    CAP_FISCAL_CREATE,
    CAP_MARKING_TRANSFER,
    CAP_MARKING_WITHDRAW,
)
from commerce.contracts import ComplianceDecision
from commerce.errors import ComplianceForbiddenError


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ComplianceRule:
    rule_id: str
    version: str
    effective_from: datetime
    effective_to: datetime | None
    jurisdiction: str
    buyer_type: str  # B2C|B2B|*
    scenario: str
    product_category: str = "*"
    conditions: Mapping[str, object] = field(default_factory=dict)
    required_actions: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    requires_hitl: bool = False
    source_ref: str = ""
    approval_state: str = "approved"
    test_cases: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "conditions", MappingProxyType(sanitize_metadata(dict(self.conditions or {}))))
        object.__setattr__(self, "required_actions", tuple(self.required_actions))
        object.__setattr__(self, "forbidden_actions", tuple(self.forbidden_actions))
        object.__setattr__(self, "required_capabilities", tuple(self.required_capabilities))
        object.__setattr__(self, "test_cases", tuple(self.test_cases))

    def active_at(self, when: datetime) -> bool:
        if when < self.effective_from:
            return False
        if self.effective_to is not None and when > self.effective_to:
            return False
        return self.approval_state == "approved"


def default_rules() -> tuple[ComplianceRule, ...]:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return (
        ComplianceRule(
            rule_id="b2c.standard",
            version="1",
            effective_from=start,
            effective_to=None,
            jurisdiction="RU",
            buyer_type="B2C",
            scenario="b2c_fulfillment",
            required_actions=("reserve", "fiscalize", "marking_withdraw_if_applicable", "ship"),
            forbidden_actions=("edo_resale_transfer",),
            required_capabilities=(CAP_FISCAL_CREATE,),
            requires_hitl=False,
            source_ref="commerce://rules/b2c.standard@1",
            test_cases=("b2c_happy_path",),
        ),
        ComplianceRule(
            rule_id="b2b.own_use",
            version="1",
            effective_from=start,
            effective_to=None,
            jurisdiction="RU",
            buyer_type="B2B",
            scenario="b2b_own_use",
            conditions={"declaration_option": 1},
            required_actions=("require_declaration", "reserve", "marking_withdraw_if_applicable", "ship"),
            forbidden_actions=("edo_resale_transfer",),
            required_capabilities=(CAP_MARKING_WITHDRAW,),
            requires_hitl=False,
            source_ref="commerce://rules/b2b.own_use@1",
            test_cases=("b2b_own_use_declaration",),
        ),
        ComplianceRule(
            rule_id="b2b.resale",
            version="1",
            effective_from=start,
            effective_to=None,
            jurisdiction="RU",
            buyer_type="B2B",
            scenario="b2b_resale",
            conditions={"declaration_option": 2},
            required_actions=("require_declaration", "edo_prepare", "edo_send", "marking_transfer", "ship"),
            forbidden_actions=("marking_withdraw_as_consumption",),
            required_capabilities=(CAP_EDO_SEND, CAP_MARKING_TRANSFER),
            requires_hitl=False,
            source_ref="commerce://rules/b2b.resale@1",
            test_cases=("b2b_resale_no_withdraw",),
        ),
        ComplianceRule(
            rule_id="risk.suspicious_own_use",
            version="1",
            effective_from=start,
            effective_to=None,
            jurisdiction="RU",
            buyer_type="B2B",
            scenario="compliance_risk",
            conditions={"risk_flag": "suspicious_own_use"},
            required_actions=("hitl_review",),
            forbidden_actions=(),
            required_capabilities=(),
            requires_hitl=True,
            source_ref="commerce://rules/risk.suspicious_own_use@1",
            test_cases=("risk_own_use_pattern",),
        ),
        ComplianceRule(
            rule_id="write.forbidden_llm_defaults",
            version="1",
            effective_from=start,
            effective_to=None,
            jurisdiction="RU",
            buyer_type="*",
            scenario="guard",
            forbidden_actions=("llm_direct_marking_withdraw", "llm_direct_fiscal_refund", "llm_direct_inventory_adjust"),
            requires_hitl=True,
            source_ref="commerce://rules/write.forbidden_llm_defaults@1",
        ),
    )


class ComplianceRulesEngine:
    def __init__(self, rules: tuple[ComplianceRule, ...] | None = None):
        self._rules = list(rules or default_rules())

    def register(self, rule: ComplianceRule) -> None:
        self._rules.append(rule)

    def select(
        self,
        *,
        buyer_type: str,
        scenario: str | None = None,
        declaration_option: int | None = None,
        risk_flag: str | None = None,
        jurisdiction: str = "RU",
        when: datetime | None = None,
        product_category: str = "*",
    ) -> ComplianceDecision:
        when = when or _utc()
        matched: list[ComplianceRule] = []
        for rule in self._rules:
            if not rule.active_at(when):
                continue
            if rule.jurisdiction not in {jurisdiction, "*"}:
                continue
            if rule.buyer_type not in {buyer_type, "*"}:
                continue
            if rule.product_category not in {product_category, "*"}:
                continue
            cond = dict(rule.conditions)
            if "declaration_option" in cond and declaration_option != cond["declaration_option"]:
                continue
            if "risk_flag" in cond and risk_flag != cond["risk_flag"]:
                continue
            if scenario and rule.scenario not in {scenario, "guard", "compliance_risk"}:
                # allow risk/guard overlays
                if rule.scenario != scenario:
                    continue
            matched.append(rule)

        if not matched:
            return ComplianceDecision(
                scenario=scenario or "unknown",
                rule_version="",
                evidence={"matched": 0},
                status="no_rule",
            )

        # Prefer specific scenario rule; risk overlays force HITL
        primary = None
        risk = None
        for r in matched:
            if r.scenario == "compliance_risk":
                risk = r
            elif scenario and r.scenario == scenario:
                primary = r
            elif primary is None and r.scenario not in {"guard"}:
                primary = r
        chosen = risk or primary or matched[0]
        required = list(chosen.required_actions)
        forbidden = list(chosen.forbidden_actions)
        caps = list(chosen.required_capabilities)
        hitl = chosen.requires_hitl
        if risk is not None and chosen is not risk and primary is not None:
            # overlay risk on primary
            required = list(primary.required_actions) + list(risk.required_actions)
            forbidden = list(set(primary.forbidden_actions) | set(risk.forbidden_actions))
            caps = list(set(primary.required_capabilities) | set(risk.required_capabilities))
            hitl = primary.requires_hitl or risk.requires_hitl
            chosen_version = f"{primary.rule_id}@{primary.version}+{risk.rule_id}@{risk.version}"
            scen = "compliance_risk"
        else:
            chosen_version = f"{chosen.rule_id}@{chosen.version}"
            scen = chosen.scenario

        return ComplianceDecision(
            scenario=scen,
            rule_version=chosen_version,
            evidence={
                "rule_id": chosen.rule_id,
                "matched_count": len(matched),
                "risk_overlay": risk.rule_id if risk and chosen is not risk else "",
            },
            required_actions=tuple(required),
            forbidden_actions=tuple(forbidden),
            required_capabilities=tuple(caps),
            requires_hitl=hitl,
            status="evaluated",
            jurisdiction=jurisdiction,
        )

    def assert_action_allowed(self, decision: ComplianceDecision, action: str) -> None:
        if action in decision.forbidden_actions:
            raise ComplianceForbiddenError("compliance_forbidden")
