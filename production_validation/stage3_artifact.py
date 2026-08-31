"""Durable Stage-3 handoff artifact — immutable PRODUCTION_VALIDATION_PASS contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STAGE3_HANDOFF_SCHEMA_VERSION = "1"
DEFAULT_STAGE3_HANDOFF_PATH = Path("production_validation") / "STAGE3_HANDOFF.json"


class Stage3HandoffArtifactError(ValueError):
    def __init__(self, code: str, *, details: dict | None = None):
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(self.code)


def load_stage3_handoff_artifact(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_STAGE3_HANDOFF_PATH
    if not target.is_file():
        raise Stage3HandoffArtifactError("stage3_artifact_missing", details={"path": str(target)})
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage3HandoffArtifactError("stage3_artifact_malformed", details={"error": type(exc).__name__}) from exc
    if not isinstance(data, dict):
        raise Stage3HandoffArtifactError("stage3_artifact_not_object")
    return data


def require_stage3_artifact_ready(data: dict[str, Any]) -> dict[str, Any]:
    schema = str(data.get("schema_version") or "")
    if schema != STAGE3_HANDOFF_SCHEMA_VERSION:
        raise Stage3HandoffArtifactError("unsupported_schema_version", details={"schema_version": schema})
    if int(data.get("stage") or 0) != 3:
        raise Stage3HandoffArtifactError("wrong_stage", details={"stage": data.get("stage")})
    if str(data.get("verdict") or "") != "PRODUCTION_VALIDATION_PASS":
        raise Stage3HandoffArtifactError("verdict_not_pass", details={"verdict": data.get("verdict")})
    if str(data.get("engineering") or "") != "PASS":
        raise Stage3HandoffArtifactError("engineering_not_pass", details={"engineering": data.get("engineering")})
    if str(data.get("live_validation") or "") != "PASS":
        raise Stage3HandoffArtifactError("live_validation_not_pass", details={"live_validation": data.get("live_validation")})
    if str(data.get("release_readiness") or "") != "READY":
        raise Stage3HandoffArtifactError("release_readiness_not_ready", details={"release_readiness": data.get("release_readiness")})
    blocked = list(data.get("blocked") or [])
    if blocked:
        raise Stage3HandoffArtifactError("blocked_nonempty", details={"blocked": blocked})
    p0 = list(data.get("p0") or [])
    p1 = list(data.get("p1") or [])
    if p0:
        raise Stage3HandoffArtifactError("p0_nonempty", details={"p0": p0})
    if p1:
        raise Stage3HandoffArtifactError("p1_nonempty", details={"p1": p1})
    if data.get("closed") is not True:
        raise Stage3HandoffArtifactError("not_closed", details={"closed": data.get("closed")})
    return data
