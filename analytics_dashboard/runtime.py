"""Build analytics dashboard runtime."""

from __future__ import annotations

from dataclasses import dataclass

from analytics_dashboard.access import AnalyticsAccessPolicy
from analytics_dashboard.service import AnalyticsDashboardService


@dataclass
class AnalyticsDashboardRuntime:
    service: AnalyticsDashboardService
    policy: AnalyticsAccessPolicy


def build_analytics_dashboard_runtime(
    *,
    integration_activation=None,
    marketplace=None,
    ops_admin=None,
) -> AnalyticsDashboardRuntime:
    policy = AnalyticsAccessPolicy()
    service = AnalyticsDashboardService(
        integration_activation=integration_activation,
        marketplace=marketplace,
        ops_admin=ops_admin,
        access=policy,
    )
    return AnalyticsDashboardRuntime(service=service, policy=policy)
