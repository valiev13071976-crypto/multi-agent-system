"""Ingestion package."""

from acquisition.ingest.target import (
    IngestionTarget,
    InMemoryIngestionTarget,
    SqliteIngestionTarget,
)

__all__ = [
    "IngestionTarget",
    "InMemoryIngestionTarget",
    "SqliteIngestionTarget",
]
