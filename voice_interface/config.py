"""Voice interface configuration."""

from __future__ import annotations

import os


def voice_interface_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get("VOICE_INTERFACE_ENABLED") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def voice_interface_db_path(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(
        source.get("VOICE_INTERFACE_DB_PATH")
        or os.path.join(source.get("PANDA_DATA_DIR") or ".", "voice_interface.sqlite")
    )


def voice_audio_dir(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(
        source.get("VOICE_AUDIO_DIR")
        or os.path.join(source.get("PANDA_DATA_DIR") or ".", "voice_audio")
    )


def max_voice_audio_bytes(env: dict | None = None) -> int:
    source = env if env is not None else os.environ
    return max(1024, int(source.get("VOICE_MAX_AUDIO_BYTES") or str(10 * 1024 * 1024)))


def max_voice_duration_seconds(env: dict | None = None) -> int:
    source = env if env is not None else os.environ
    return max(1, int(source.get("VOICE_MAX_DURATION_SECONDS") or "120"))


def tts_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get("VOICE_TTS_ENABLED") or "true").strip().lower() in {"1", "true", "yes", "on"}
