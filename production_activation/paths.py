"""Canonical durable path for Stage-5 production activation state."""

from __future__ import annotations

import os
from pathlib import Path

from production_activation.sqlite_store import SqliteProductionActivationStore


def resolve_production_activation_db_path(env: dict | None = None) -> str:
    """Resolve Stage-5 SQLite path under PANDA_DATA_DIR (production: /data).

    Override: PRODUCTION_ACTIVATION_DB_PATH
    Default:  $PANDA_DATA_DIR/production_activation.sqlite
              (falls back to ./data outside production when PANDA_DATA_DIR unset)
    """
    source = env if env is not None else os.environ
    explicit = str(source.get("PRODUCTION_ACTIVATION_DB_PATH") or "").strip()
    if explicit:
        return explicit

    data_dir = str(source.get("PANDA_DATA_DIR") or source.get("DATA_DIR") or "").strip()
    if not data_dir:
        env_name = str(source.get("PANDA_ENV") or source.get("ENVIRONMENT") or "development").strip().lower()
        data_dir = "/data" if env_name in {"production", "prod"} else "./data"
    return os.path.join(data_dir, "production_activation.sqlite")


def open_production_activation_store(env: dict | None = None) -> SqliteProductionActivationStore:
    """Open the canonical durable Stage-5 store (never :memory: for production CLI)."""
    path = resolve_production_activation_db_path(env)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteProductionActivationStore(path=path)
