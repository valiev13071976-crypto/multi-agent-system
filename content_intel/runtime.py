"""Content Intelligence runtime composition."""

from __future__ import annotations

import os

from content_intel.service import ContentIntelligenceService
from content_intel.sqlite_store import SqliteContentStore


def content_intel_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("CONTENT_INTEL_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


class ContentIntelligenceRuntime:
    def __init__(self, *, service: ContentIntelligenceService, enabled: bool = True):
        self.service = service
        self.enabled = bool(enabled)

    def health(self) -> dict:
        return {"content_intel_status": "healthy" if self.enabled else "disabled", "enabled": self.enabled}


def build_content_intelligence_runtime(
    *,
    env: dict | None = None,
    knowledge_service=None,
    tool_gateway=None,
    observability=None,
    store=None,
    db_path: str | None = None,
    product_media_service=None,
) -> ContentIntelligenceRuntime | None:
    if not content_intel_enabled(env):
        return None
    source = env if env is not None else os.environ
    path = db_path or str(source.get("CONTENT_INTEL_DB_PATH") or ":memory:")
    content_store = store or SqliteContentStore(path)
    service = ContentIntelligenceService(
        content_store,
        knowledge_service=knowledge_service,
        tool_gateway=tool_gateway,
        observability=observability,
        product_media_service=product_media_service,
    )
    return ContentIntelligenceRuntime(service=service, enabled=True)
