"""Runtime wiring for Product Media Intelligence."""

from __future__ import annotations

from product_media.observability import MediaObservability
from product_media.service import ProductMediaService
from product_media.similarity import TenantSimilarityIndex
from product_media.sqlite_store import SqliteMediaStore


def build_product_media_runtime(*, db_path: str = ":memory:", production_bundle=None, env: dict | None = None) -> ProductMediaService:
    import os

    source = env if env is not None else os.environ
    store = SqliteMediaStore(db_path)
    if production_bundle is not None:
        generator = production_bundle.image_provider
    else:
        from integrations.production.adapters.media import build_image_provider

        generator = build_image_provider(source)
    return ProductMediaService(store=store, similarity_index=TenantSimilarityIndex(), obs=MediaObservability(), generator=generator)
