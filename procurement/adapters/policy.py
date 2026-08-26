"""External research policy for procurement adapters."""

from __future__ import annotations

from procurement.adapters.models import PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION


class ProcurementExternalResearchPolicy:
    policy_version = PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION

    def __init__(
        self,
        *,
        enabled: bool = False,
        max_queries: int = 2,
        max_results_per_query: int = 5,
        max_total_results: int = 10,
        min_internal_suppliers_before_skip: int = 2,
        allowed_countries: tuple[str, ...] = (),
        allowed_source_types: tuple[str, ...] = ("search_provider", "registered_catalog"),
        freshness_required: bool = False,
        timeout_seconds: float = 10.0,
        catalog_max_items: int = 50,
        rfq_draft_max_chars: int = 8000,
    ):
        self.enabled = bool(enabled)
        self.max_queries = max(0, int(max_queries))
        self.max_results_per_query = max(1, min(int(max_results_per_query), 20))
        self.max_total_results = max(1, min(int(max_total_results), 50))
        self.min_internal_suppliers_before_skip = max(0, int(min_internal_suppliers_before_skip))
        self.allowed_countries = tuple(c.upper() for c in allowed_countries)
        self.allowed_source_types = tuple(allowed_source_types)
        self.freshness_required = bool(freshness_required)
        self.timeout_seconds = float(timeout_seconds)
        self.catalog_max_items = max(1, min(int(catalog_max_items), 200))
        self.rfq_draft_max_chars = max(500, min(int(rfq_draft_max_chars), 50_000))

    def should_call_external(self, *, internal_supplier_count: int, force: bool = False) -> bool:
        if not self.enabled:
            return False
        if force:
            return True
        return internal_supplier_count < self.min_internal_suppliers_before_skip


def external_research_policy_snapshot(policy: ProcurementExternalResearchPolicy | None = None) -> dict:
    p = policy or ProcurementExternalResearchPolicy()
    return {
        "procurement_external_research_policy_version": PROCUREMENT_EXTERNAL_RESEARCH_POLICY_VERSION,
        "enabled_default": False,
        "max_queries": p.max_queries,
        "max_results_per_query": p.max_results_per_query,
        "max_total_results": p.max_total_results,
        "min_internal_suppliers_before_skip": p.min_internal_suppliers_before_skip,
        "no_arbitrary_url": True,
        "no_auto_send": True,
        "rules": [
            "internal_first",
            "disabled_by_default",
            "tool_gateway_only",
            "ssrf_enforced",
            "draft_only_rfq",
        ],
    }
