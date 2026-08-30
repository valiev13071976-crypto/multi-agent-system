"""Build UI Chat runtime from composed platform services."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ui_chat.attachments import AttachmentRouter, AttachmentLimits
from ui_chat.observability import UIChatObservability
from ui_chat.service import UIChatService
from ui_chat.sqlite_store import SqliteUIChatStore
from ui_chat.voice.stt import FakeSpeechToTextProvider
from ui_chat.voice.tts import FakeTextToSpeechProvider


@dataclass
class UIChatRuntime:
    service: UIChatService
    store: SqliteUIChatStore

    def close(self) -> None:
        self.store.close()


def build_ui_chat_runtime(
    *,
    side_effect_runtime=None,
    workflow_engine=None,
    run_router=None,
    context_manager=None,
    env: dict | None = None,
    production_bundle=None,
) -> UIChatRuntime:
    source = env if env is not None else os.environ
    db_path = source.get("UI_CHAT_DB_PATH") or source.get("SIDE_EFFECT_DB_PATH") or "data/ui_chat.sqlite"
    if db_path.endswith(".sqlite"):
        db_path = db_path.replace(".sqlite", "_chat.sqlite")
    store = SqliteUIChatStore(db_path)

    document_service = None
    data_intel_service = None
    product_media_service = None
    workflow_runtime = None
    if side_effect_runtime is not None:
        doc_rt = getattr(side_effect_runtime, "document_runtime", None)
        if doc_rt is not None:
            document_service = getattr(doc_rt, "service", None)
        data_rt = getattr(side_effect_runtime, "data_intelligence_runtime", None)
        if data_rt is not None:
            data_intel_service = getattr(data_rt, "service", None)
        media_rt = getattr(side_effect_runtime, "product_media_runtime", None)
        if media_rt is not None:
            product_media_service = getattr(media_rt, "service", None)
        workflow_runtime = getattr(side_effect_runtime, "workflow_runtime", None)

    def _scope_factory(tenant_id: str):
        from memory.models import MemoryScope, SCOPE_PROJECT

        return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=tenant_id)

    attachment_router = AttachmentRouter(
        limits=AttachmentLimits.from_env(source),
        document_service=document_service,
        data_intel_service=data_intel_service,
        product_media_service=product_media_service,
    )
    if production_bundle is not None:
        stt_provider = production_bundle.stt_provider
        tts_provider = production_bundle.tts_provider
    else:
        from integrations.production.adapters.speech import build_speech_providers

        stt_provider, tts_provider = build_speech_providers(source)
    service = UIChatService(
        store,
        attachment_router=attachment_router,
        stt_provider=stt_provider,
        tts_provider=tts_provider,
        workflow_engine=workflow_engine,
        run_router=run_router,
        context_manager=context_manager,
        workflow_runtime=workflow_runtime,
        memory_scope_factory=_scope_factory,
        obs=UIChatObservability(),
    )
    return UIChatRuntime(service=service, store=store)
