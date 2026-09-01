# Voice Interface

Canonical voice input/output transport for Panda Multi-Agent over the closed **Business Assistant API / Chat**.

## Architecture

```
User Speech
  → Voice Interface (/api/v1/voice)
  → Audio validation
  → STT abstraction (fixture or production adapter)
  → Normalized transcript
  → Business Assistant API / Chat
  → Result (text authoritative)
  → Optional TTS abstraction
  → Audio artifact reference
```

Voice does **not** call LLM chat/completions, marketplace APIs, or workflow internals directly.

## STT / TTS separation

| Capability | Role |
|------------|------|
| **STT** | Required for voice input; failure blocks BA submission |
| **TTS** | Optional; failure does not change successful BA status |

Providers: `ui_chat.voice` fake implementations for tests; `integrations.production.adapters.speech.build_speech_providers()` for production.

## Supported audio

- WAV, MP3/MPEG, M4A/MP4, OGG/Opus, WEBM
- Max size: `VOICE_MAX_AUDIO_BYTES` (default 10 MB)
- Blocked: executables, path traversal filenames

## Configuration (names only)

| Variable | Purpose |
|----------|---------|
| `VOICE_INTERFACE_ENABLED` | Enable module (default true) |
| `VOICE_INTERFACE_DB_PATH` | Transport metadata SQLite |
| `VOICE_AUDIO_DIR` | TTS artifact storage |
| `VOICE_MAX_AUDIO_BYTES` | Upload limit |
| `VOICE_TTS_ENABLED` | Enable TTS on completed results |
| `SPEECH_PROVIDER` | `fake` or production speech adapter |
| `SPEECH_API_KEY` / `OPENAI_API_KEY` | Production STT/TTS credentials |

## API

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/voice/transcribe` | STT only |
| `POST /api/v1/voice/requests` | Audio → STT → BA request |
| `GET /api/v1/voice/requests/{id}` | Status, transcript, result, TTS ref |
| `POST /api/v1/voice/requests/{id}/approve` | Canonical HITL approve |
| `POST /api/v1/voice/requests/{id}/reject` | Reject |
| `POST /api/v1/voice/requests/{id}/cancel` | Cancel |
| `GET /api/v1/voice/audio/{artifact_id}` | TTS audio download |

Auth: existing `X-API-Key` / RBAC via `get_security_context`.

## HITL / spoken approval

- Business commands (e.g. "Опубликуй товар") create plans and **do not** auto-approve
- Explicit spoken approval phrases (e.g. "да, подтверждаю") approve **only when exactly one** pending approval exists in the conversation
- Ambiguous approval fails closed (`vi_ambiguous_spoken_approval`)

## Idempotency

`idempotency_key` on voice requests maps to canonical BA idempotency; duplicates return the same logical request.

## Engineering vs live

| Flag | Meaning |
|------|---------|
| `VOICE_INTERFACE_ENGINEERING_READY` | Implementation + tests complete |
| `VOICE_STT_LIVE_ACTIVE` | Real STT provider verified |
| `VOICE_TTS_LIVE_ACTIVE` | Real TTS provider verified |

Fixtures use `PANDA_STT_TEST:` marker bytes for deterministic transcripts in tests.

## Tests

```bash
python -m unittest tests.test_voice_interface_closure -v
```

## Deferred

- Browser microphone UI in closed Web Interface
- Telegram voice message wiring (contracts ready)
- Long-audio async transcription queue
- Live paid STT/TTS activation
