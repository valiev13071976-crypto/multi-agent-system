"""Block 4 — canonical realtime multimodal conversation session layer.

One session state machine (state_machine.py), one event contract
(events.py), and one orchestration bridge (bridge.py) shared by every
transport (currently WebSocket, router.py). Reuses the existing,
already-CLOSED Business Assistant conversation pipeline
(business_assistant_api.service.BusinessAssistantApiService.submit_async)
and STT/TTS provider wiring (ui_chat.voice.*,
integrations.production.adapters.speech.build_speech_providers) unchanged --
voice is another input/output modality on the SAME conversation, tool
routing, authorization, HITL and idempotency boundary as text.
"""
