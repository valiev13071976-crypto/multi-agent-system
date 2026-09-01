"""Runtime wiring for Voice interface."""

from __future__ import annotations

import os
from dataclasses import dataclass

from business_assistant_api.runtime import build_business_assistant_api_runtime
from business_assistant_api.service import BusinessAssistantApiService
from voice_interface.config import voice_audio_dir, voice_interface_db_path, voice_interface_enabled
from voice_interface.service import VoiceInterfaceService
from voice_interface.store import SqliteVoiceInterfaceStore


@dataclass
class VoiceInterfaceRuntime:
    service: VoiceInterfaceService
    store: SqliteVoiceInterfaceStore
    ba_api: BusinessAssistantApiService

    def close(self) -> None:
        self.service.close()


def build_voice_interface_runtime(
    *,
    env: dict | None = None,
    ba_api: BusinessAssistantApiService | None = None,
    db_path: str | None = None,
) -> VoiceInterfaceRuntime:
    env = dict(env or os.environ)
    if not voice_interface_enabled(env):
        raise RuntimeError("VOICE_INTERFACE_ENABLED is false")
    path = db_path or voice_interface_db_path(env)
    store = SqliteVoiceInterfaceStore(path)
    if ba_api is None:
        ba_rt = build_business_assistant_api_runtime(env=env, db_path=env.get("BA_API_DB_PATH"))
        ba_api = ba_rt.service
    audio_dir = voice_audio_dir(env)
    os.makedirs(audio_dir, exist_ok=True)
    svc = VoiceInterfaceService(store=store, ba_api=ba_api, audio_dir=audio_dir, env=env)
    return VoiceInterfaceRuntime(service=svc, store=store, ba_api=ba_api)
