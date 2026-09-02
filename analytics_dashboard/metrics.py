"""Governed metric registry."""

from __future__ import annotations

from analytics_dashboard.models import MetricDefinition

METRIC_REGISTRY: dict[str, MetricDefinition] = {
    "commerce.orders.count": MetricDefinition(
        metric_id="commerce.orders.count",
        display_name="Order Count",
        domain="commerce",
        source="fixture_commerce",
        unit="count",
        aggregation="sum",
        dimensions=("marketplace", "channel"),
    ),
    "commerce.revenue": MetricDefinition(
        metric_id="commerce.revenue",
        display_name="Revenue",
        domain="commerce",
        source="fixture_commerce",
        unit="money",
        aggregation="sum",
        dimensions=("marketplace", "currency"),
    ),
    "commerce.average_order_value": MetricDefinition(
        metric_id="commerce.average_order_value",
        display_name="Average Order Value",
        domain="commerce",
        source="derived",
        unit="money",
        aggregation="avg",
        dimensions=("marketplace", "currency"),
    ),
    "commerce.margin.gross": MetricDefinition(
        metric_id="commerce.margin.gross",
        display_name="Gross Margin",
        domain="commerce",
        source="marketplace_economics",
        unit="money",
        aggregation="sum",
        dimensions=("marketplace",),
        description="Uses existing marketplace economics — not net profit",
    ),
    "commerce.stock.units": MetricDefinition(
        metric_id="commerce.stock.units",
        display_name="Stock Units",
        domain="commerce",
        source="fixture_inventory",
        unit="count",
        aggregation="sum",
        dimensions=("marketplace", "warehouse", "sku"),
    ),
    "marketplace.price_floor_risk": MetricDefinition(
        metric_id="marketplace.price_floor_risk",
        display_name="Price Floor Risk SKUs",
        domain="marketplace",
        source="marketplace_economics",
        unit="count",
        aggregation="count",
        dimensions=("marketplace", "sku"),
    ),
    "platform.workflow.success_rate": MetricDefinition(
        metric_id="platform.workflow.success_rate",
        display_name="Workflow Success Rate",
        domain="platform",
        source="fixture_runtime",
        unit="ratio",
        aggregation="avg",
    ),
    "platform.queue.depth": MetricDefinition(
        metric_id="platform.queue.depth",
        display_name="Queue Depth",
        domain="platform",
        source="fixture_runtime",
        unit="count",
        aggregation="last",
        dimensions=("queue",),
    ),
    "ai.requests": MetricDefinition(
        metric_id="ai.requests",
        display_name="AI Requests",
        domain="ai",
        source="fixture_finops",
        unit="count",
        aggregation="sum",
        dimensions=("provider", "model"),
    ),
    "ai.cost": MetricDefinition(
        metric_id="ai.cost",
        display_name="AI Cost",
        domain="finops",
        source="fixture_finops",
        unit="money",
        aggregation="sum",
        dimensions=("provider", "model", "currency"),
    ),
    "integrations.success_rate": MetricDefinition(
        metric_id="integrations.success_rate",
        display_name="Integration Success Rate",
        domain="integrations",
        source="integration_activation",
        unit="ratio",
        aggregation="avg",
        dimensions=("provider",),
    ),
    "business_assistant.requests": MetricDefinition(
        metric_id="business_assistant.requests",
        display_name="BA Requests",
        domain="business_assistant",
        source="fixture_ba",
        unit="count",
        aggregation="sum",
    ),
    "productivity.email.operations": MetricDefinition(
        metric_id="productivity.email.operations",
        display_name="Email Operations",
        domain="productivity",
        source="integration_activation",
        unit="count",
        aggregation="sum",
        description="Operational counts only — no message bodies",
    ),
    "productivity.calendar.operations": MetricDefinition(
        metric_id="productivity.calendar.operations",
        display_name="Calendar Operations",
        domain="productivity",
        source="integration_activation",
        unit="count",
        aggregation="sum",
    ),
    "productivity.crm.operations": MetricDefinition(
        metric_id="productivity.crm.operations",
        display_name="CRM Operations",
        domain="productivity",
        source="integration_activation",
        unit="count",
        aggregation="sum",
    ),
}

ALLOWED_FILTER_KEYS = frozenset(
    {
        "marketplace",
        "provider",
        "sku",
        "product_id",
        "channel",
        "warehouse",
        "model",
        "queue",
        "currency",
    }
)

ALLOWED_GROUP_BY = frozenset({"marketplace", "provider", "sku", "channel", "warehouse", "model", "day", "week"})


def get_metric(metric_id: str) -> MetricDefinition | None:
    return METRIC_REGISTRY.get(metric_id)
