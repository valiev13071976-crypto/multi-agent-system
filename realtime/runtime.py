"""Runtime wiring for the realtime multimodal session layer."""

from __future__ import annotations

import os
from dataclasses import dataclass

from business_assistant_api.service import BusinessAssistantApiService
from finops.service import FinOpsService
from integrations.production.adapters.speech import build_speech_providers
from ui_chat.voice.stt import SpeechToTextProvider
from ui_chat.voice.tts import TextToSpeechProvider

from personalization.service import PersonalizationService
from realtime.bridge import RealtimeConversationBridge


@dataclass
class RealtimeRuntime:
    bridge: RealtimeConversationBridge


def realtime_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get("REALTIME_ENABLED") or "true").strip().lower() in {"1", "true", "yes", "on"}


def build_realtime_runtime(
    *,
    ba_api: BusinessAssistantApiService,
    personalization: PersonalizationService | None = None,
    finops: FinOpsService | None = None,
    env: dict | None = None,
    stt: SpeechToTextProvider | None = None,
    tts: TextToSpeechProvider | None = None,
) -> RealtimeRuntime:
    env = dict(env or os.environ)
    if stt is None or tts is None:
        built_stt, built_tts = build_speech_providers(env)
        stt = stt or built_stt
        tts = tts or built_tts
    bridge = RealtimeConversationBridge(
        ba_api=ba_api, stt=stt, tts=tts, personalization=personalization, finops=finops
    )
    return RealtimeRuntime(bridge=bridge)
