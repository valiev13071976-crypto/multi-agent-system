"""Privacy-safe media observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MediaObservability:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, name: str, *, status: str = "ok", metadata: dict | None = None) -> None:
        safe = {}
        for k, v in (metadata or {}).items():
            if k in ("blob", "bytes", "base64", "raw", "exif_raw"):
                continue
            if isinstance(v, (bytes, bytearray)):
                continue
            safe[k] = v
        self.events.append((name, {"status": status, **safe}))
