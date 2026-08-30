"""Build production foundation runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass

from production_foundation.config import assert_production_startup_safe, resolve_production_config
from production_foundation.service import ProductionFoundationService


@dataclass
class ProductionFoundationRuntime:
    service: ProductionFoundationService

    def close(self) -> None:
        pass


def build_production_foundation_runtime(
    *,
    env: dict | None = None,
    side_effect_connection=None,
    saas_store=None,
    persistence_ready: bool = False,
) -> ProductionFoundationRuntime:
    source = env if env is not None else os.environ
    cfg = resolve_production_config(source)
    service = ProductionFoundationService(
        config=cfg,
        side_effect_connection=side_effect_connection,
        saas_store=saas_store,
        persistence_ready=persistence_ready,
    )
    return ProductionFoundationRuntime(service=service)


def initialize_production_foundation(
    *,
    env: dict | None = None,
    side_effect_connection=None,
    saas_store=None,
    persistence_ready: bool = False,
    fail_closed: bool = True,
) -> ProductionFoundationRuntime:
    assert_production_startup_safe(env)
    rt = build_production_foundation_runtime(
        env=env,
        side_effect_connection=side_effect_connection,
        saas_store=saas_store,
        persistence_ready=persistence_ready,
    )
    rt.service.initialize()
    if fail_closed and rt.service._migration_report and rt.service._migration_report.overall == "FAIL":
        from production_foundation.errors import PF_MIGRATION_FAILED, ProductionFoundationError

        raise ProductionFoundationError(PF_MIGRATION_FAILED)
    return rt
