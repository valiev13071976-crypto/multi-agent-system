"""Provider-neutral deterministic embedding interface."""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod

from knowledge.platform_models import EMBEDDING_DIM, EMBEDDING_MODEL_FAKE


class EmbeddingProvider(ABC):
    model_id: str
    version: str
    dimension: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        raise NotImplementedError


class DeterministicEmbeddingProvider(EmbeddingProvider):
    """Local fake embeddings — deterministic, no vendor credentials."""

    model_id = EMBEDDING_MODEL_FAKE
    version = "1"
    dimension = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        out: list[tuple[float, ...]] = []
        for text in texts:
            digest = hashlib.sha256(str(text or "").encode("utf-8")).digest()
            raw = [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(self.dimension)]
            norm = math.sqrt(sum(x * x for x in raw)) or 1.0
            out.append(tuple(x / norm for x in raw))
        return out


class NullEmbeddingProvider(EmbeddingProvider):
    model_id = "null"
    version = "0"
    dimension = 0

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        return [tuple() for _ in texts]


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if not a or not b or len(a) != len(b):
        raise ValueError("incompatible_embedding_dimension")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
