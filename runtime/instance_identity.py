"""Stable per-process runtime instance identity (Scale 3.28/3.29/3.31).

Distributed coordination and shared fleet health need a stable identity for
*this* runtime process, distinct from the per-lease ``worker_id`` used for
queue claim fencing (a single instance may run several worker pool slots,
each with its own worker_id, all sharing one instance_id).

Trust boundary: instance identity is NEVER derived from untrusted request
input. It is either supplied via trusted deployment configuration
(``RUNTIME_INSTANCE_ID`` env var, set by the orchestrator) or generated once
per process from local, non-user-controlled sources (hostname/pid/uuid4).
"""

from __future__ import annotations

import os
import socket
import uuid
from typing import Mapping

__all__ = ["generate_instance_id", "instance_id", "reset_for_tests"]

_CACHED_INSTANCE_ID: str | None = None


def generate_instance_id() -> str:
    """Generate a fresh, process-local, non-user-controlled instance id."""

    try:
        host = socket.gethostname() or "host"
    except Exception:
        host = "host"
    # Keep it short and label-safe (bounded cardinality when used in metrics).
    host = "".join(ch for ch in host if ch.isalnum() or ch in "-_")[:24] or "host"
    return f"{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def instance_id(env: Mapping | None = None) -> str:
    """Return this process's stable instance_id.

    Trusted override via ``RUNTIME_INSTANCE_ID`` (deployment configuration);
    otherwise a value generated once and cached for the process lifetime.
    """

    global _CACHED_INSTANCE_ID
    source = env if env is not None else os.environ
    override = source.get("RUNTIME_INSTANCE_ID")
    if override and str(override).strip():
        return str(override).strip()
    if _CACHED_INSTANCE_ID is None:
        _CACHED_INSTANCE_ID = generate_instance_id()
    return _CACHED_INSTANCE_ID


def reset_for_tests() -> None:
    """Test-only: clear the cached generated instance_id."""

    global _CACHED_INSTANCE_ID
    _CACHED_INSTANCE_ID = None
