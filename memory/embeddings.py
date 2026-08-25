"""Optional embedding provider — Null by default (no network)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class NullEmbeddingProvider:
    """No external/local model calls."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


class NullVectorIndex:
    def upsert(self, *args, **kwargs) -> None:
        return None

    def search(self, *args, **kwargs) -> tuple:
        return ()
