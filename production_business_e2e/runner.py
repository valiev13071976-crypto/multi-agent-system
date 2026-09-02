"""Production Business E2E scenario runner."""

from __future__ import annotations

from production_business_e2e.evidence import summarize
from production_business_e2e.harness import build_e2e_world
from production_business_e2e.models import E2EEvidence
from production_business_e2e.scenarios import (
    run_analytics_business_question,
    run_governed_marketplace_write,
    run_marketplace_economics,
    run_scheduled_to_analytics,
    run_supplier_analysis,
)

CANONICAL_RUNNERS = {
    "A": run_supplier_analysis,
    "B": run_marketplace_economics,
    "E": run_governed_marketplace_write,
    "K": run_analytics_business_question,
    "L": run_scheduled_to_analytics,
}


def run_scenario(scenario_id: str, world=None) -> E2EEvidence:
    world = world or build_e2e_world()
    fn = CANONICAL_RUNNERS.get(scenario_id)
    if fn is None:
        raise KeyError(scenario_id)
    return fn(world)


def run_canonical_suite(world=None) -> dict:
    world = world or build_e2e_world()
    results = [fn(world) for fn in CANONICAL_RUNNERS.values()]
    return summarize(results)
