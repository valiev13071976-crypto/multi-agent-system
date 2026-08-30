"""Product media persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod

from product_media.platform_models import (
    MediaAssetVersion,
    MediaJob,
    ProductMediaLink,
    ProductMediaSet,
)


class MediaStore(ABC):
    @abstractmethod
    def save_version(self, version: MediaAssetVersion, *, blob: bytes) -> None: ...

    @abstractmethod
    def get_version(self, version_id: str, *, tenant_id: str) -> MediaAssetVersion | None: ...

    @abstractmethod
    def get_blob(self, version_id: str, *, tenant_id: str) -> bytes | None: ...

    @abstractmethod
    def tombstone_version(self, version_id: str, *, tenant_id: str) -> bool: ...

    @abstractmethod
    def save_link(self, link: ProductMediaLink) -> None: ...

    @abstractmethod
    def get_links(self, *, tenant_id: str, media_version_id: str) -> list[ProductMediaLink]: ...

    @abstractmethod
    def save_media_set(self, media_set: ProductMediaSet) -> None: ...

    @abstractmethod
    def get_media_set(self, set_id: str, *, tenant_id: str) -> ProductMediaSet | None: ...

    @abstractmethod
    def save_job(self, job: MediaJob) -> None: ...

    @abstractmethod
    def get_job(self, job_id: str, *, tenant_id: str) -> MediaJob | None: ...
