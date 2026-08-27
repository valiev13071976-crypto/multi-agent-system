"""Composition bootstrap for Data Acquisition & Parsing Platform."""

from __future__ import annotations

from acquisition.parsers import build_default_parser_registry
from acquisition.registry import SourceRegistry
from acquisition.schedule import AcquisitionScheduler
from acquisition.service import AcquisitionService
from acquisition.store import InMemoryAcquisitionStore
from workflow.schedule import WorkflowScheduler


def build_acquisition_runtime(
    *,
    tool_gateway=None,
    workflow_scheduler: WorkflowScheduler | None = None,
    freeze_sources: bool = False,
) -> AcquisitionService:
    """Build acquisition service. Core works with ToolGateway optional for local ingest tests."""
    sources = SourceRegistry()
    store = InMemoryAcquisitionStore()
    parsers = build_default_parser_registry()
    scheduler = AcquisitionScheduler(workflow_scheduler or WorkflowScheduler())
    service = AcquisitionService(
        source_registry=sources,
        store=store,
        parser_registry=parsers,
        tool_gateway=tool_gateway,
        scheduler=scheduler,
    )
    if freeze_sources:
        sources.freeze()
    return service
