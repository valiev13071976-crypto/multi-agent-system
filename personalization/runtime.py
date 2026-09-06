"""Runtime wiring for Personalization."""

from __future__ import annotations

import os
from dataclasses import dataclass

from integrations.production.adapters.speech import build_speech_providers
from ui_chat.voice.tts import TextToSpeechProvider

from personalization.service import PersonalizationService
from personalization.store import SqlitePersonalizationStore


@dataclass
class PersonalizationRuntime:
    service: PersonalizationService
    store: SqlitePersonalizationStore

    def close(self) -> None:
        self.service.close()


def personalization_db_path(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(
        source.get("PERSONALIZATION_DB_PATH")
        or os.path.join(source.get("PANDA_DATA_DIR") or ".", "personalization.sqlite")
    )


def build_personalization_runtime(
    *,
    env: dict | None = None,
    db_path: str | None = None,
    tts: TextToSpeechProvider | None = None,
) -> PersonalizationRuntime:
    env = dict(env or os.environ)
    path = db_path or personalization_db_path(env)
    store = SqlitePersonalizationStore(path)
    if tts is None:
        _, tts = build_speech_providers(env)
    svc = PersonalizationService(store=store, tts=tts)
    return PersonalizationRuntime(service=svc, store=store)
