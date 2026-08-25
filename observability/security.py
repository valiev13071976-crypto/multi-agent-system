"""Centralized observability metadata sanitization."""

from __future__ import annotations

import json
from typing import Any, Mapping

from security.redaction import redact

MAX_DEPTH = 4
MAX_LIST_LEN = 32
MAX_STRING_LEN = 512
DEFAULT_MAX_BYTES = 4096

FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "bearer",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "token",
        "encryption_key",
        "panda_encryption_key",
        "github_write_token",
        "raw_prompt",
        "prompt",
        "raw_args",
        "arguments",
        "tool_args",
        "capability_token",
        "permit_token",
        "permit_material",
        "signature",
        "raw_body",
        "headers",
        "cookie",
        "cookies",
    }
)

_PRIMITIVES = (str, int, float, bool, type(None))


def sanitize_observability_metadata(
    metadata: Mapping | None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = MAX_DEPTH,
) -> tuple[dict[str, Any], bool]:
    """Return (cleaned_dict, truncated). Never raises on bad input."""

    truncated = False

    def _walk(node: Any, depth: int) -> Any:
        nonlocal truncated
        if depth > max_depth:
            truncated = True
            return None
        if isinstance(node, _PRIMITIVES):
            if isinstance(node, str):
                text = redact(node)
                if len(text) > MAX_STRING_LEN:
                    truncated = True
                    return text[:MAX_STRING_LEN]
                return text
            return node
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for key, value in list(node.items())[:64]:
                lowered = str(key).lower()
                if lowered in FORBIDDEN_KEYS or any(
                    part in lowered for part in FORBIDDEN_KEYS
                ):
                    truncated = True
                    continue
                cleaned = _walk(value, depth + 1)
                if cleaned is not None or value is None:
                    out[str(key)] = cleaned
            return out
        if isinstance(node, (list, tuple)):
            items = list(node)[:MAX_LIST_LEN]
            if len(node) > MAX_LIST_LEN:
                truncated = True
            return [_walk(item, depth + 1) for item in items]
        # No repr(object) — drop opaque values.
        truncated = True
        return None

    cleaned = _walk(dict(metadata or {}), 0)
    if not isinstance(cleaned, dict):
        cleaned = {}
    try:
        encoded = json.dumps(cleaned, separators=(",", ":"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        truncated = True
        return {}, True
    if len(encoded.encode("utf-8")) > int(max_bytes):
        truncated = True
        # Drop largest string values first.
        keys = sorted(
            cleaned.keys(),
            key=lambda k: len(str(cleaned.get(k, ""))),
            reverse=True,
        )
        slim = dict(cleaned)
        for key in keys:
            slim.pop(key, None)
            try:
                blob = json.dumps(
                    slim, separators=(",", ":"), sort_keys=True, default=str
                )
            except (TypeError, ValueError):
                continue
            if len(blob.encode("utf-8")) <= int(max_bytes):
                cleaned = slim
                break
        else:
            cleaned = {"metadata_truncated": True}
    return cleaned, truncated
