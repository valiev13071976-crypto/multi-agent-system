"""Stage-4 handoff artifact loader — authoritative CONTROLLED_LAUNCH_PASS contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from production_activation.errors import BLOCKED_BY_PREVIOUS_STAGE, STALE_HANDOFF, ProductionActivationError

DEFAULT_STAGE4_HANDOFF_PATH = Path("controlled_launch") / "STAGE4_HANDOFF.json"


def load_stage4_handoff_artifact(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_STAGE4_HANDOFF_PATH
    if not target.is_file():
        raise ProductionActivationError(
            BLOCKED_BY_PREVIOUS_STAGE,
            details={"stage4_artifact": "missing", "path": str(target)},
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionActivationError(
            BLOCKED_BY_PREVIOUS_STAGE,
            details={"stage4_artifact": "malformed", "error": type(exc).__name__},
        ) from exc
    if not isinstance(data, dict):
        raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"stage4_artifact": "not_object"})
    return data


def require_stage4_artifact_ready(data: dict[str, Any], *, release_identity: str = "") -> dict[str, Any]:
    verdict = str(data.get("verdict") or "")
    engineering = str(data.get("engineering") or "")
    controlled = str(data.get("controlled_launch") or "")
    eligibility = str(data.get("go_live_eligibility") or "")
    active = bool(data.get("go_live_active"))
    blocked = list(data.get("blocked") or [])
    p0 = list(data.get("p0") or [])
    p1 = list(data.get("p1") or [])

    if verdict != "CONTROLLED_LAUNCH_PASS":
        raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"verdict": verdict})
    if engineering != "PASS" or controlled != "PASS":
        raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"engineering": engineering, "controlled_launch": controlled})
    if eligibility != "ELIGIBLE":
        raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"go_live_eligibility": eligibility})
    if active:
        raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"go_live_active": True, "reason": "stage4_must_not_be_active"})
    if blocked:
        raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"blocked": blocked})
    if p0 or p1:
        raise ProductionActivationError(BLOCKED_BY_PREVIOUS_STAGE, details={"p0": p0, "p1": p1})
    artifact_release = str(data.get("release_identity") or "")
    if release_identity and artifact_release and release_identity != artifact_release:
        raise ProductionActivationError(STALE_HANDOFF, details={"expected": artifact_release, "got": release_identity})
    return data
