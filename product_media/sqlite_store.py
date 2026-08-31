"""SQLite-backed media store with blob table."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from types import MappingProxyType

from product_media.platform_models import (
    MediaAssetVersion,
    MediaJob,
    ProductMediaLink,
    ProductMediaSet,
    STATUS_TOMBSTONED,
)
from product_media.store import MediaStore


def _jsonable(value):
    if isinstance(value, MappingProxyType):
        return dict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _version_payload(version: MediaAssetVersion) -> dict:
    return {
        "media_id": version.media_id,
        "version_id": version.version_id,
        "tenant_id": version.tenant_id,
        "content_hash": version.content_hash,
        "mime_type": version.mime_type,
        "media_type": version.media_type,
        "byte_size": version.byte_size,
        "width": version.width,
        "height": version.height,
        "status": version.status,
        "operation": version.operation,
        "parent_version_id": version.parent_version_id,
        "transform_profile": version.transform_profile,
        "provider_id": version.provider_id,
        "artifact_id": version.artifact_id,
        "created_at": version.created_at.isoformat(),
        "metadata_safe": dict(version.metadata_safe),
        "rights_status": version.rights_status,
        "source_content_hash": version.source_content_hash,
        "recipe_id": version.recipe_id,
        "recipe_version": version.recipe_version,
        "target_profile_id": version.target_profile_id,
    }


class SqliteMediaStore(MediaStore):
    def __init__(self, path: str = ":memory:"):
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS media_versions (
                version_id TEXT PRIMARY KEY,
                media_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_blobs (
                version_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                blob BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_links (
                link_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_sets (
                set_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_jobs (
                job_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_media_versions_tenant ON media_versions(tenant_id);
            """
        )
        self._conn.commit()

    def save_version(self, version: MediaAssetVersion, *, blob: bytes) -> None:
        payload = _version_payload(version)
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO media_versions(version_id, media_id, tenant_id, payload, status) VALUES (?,?,?,?,?)",
            (version.version_id, version.media_id, version.tenant_id, json.dumps(payload), version.status),
        )
        cur.execute(
            "INSERT OR REPLACE INTO media_blobs(version_id, tenant_id, blob) VALUES (?,?,?)",
            (version.version_id, version.tenant_id, blob),
        )
        self._conn.commit()

    def get_version(self, version_id: str, *, tenant_id: str) -> MediaAssetVersion | None:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT payload FROM media_versions WHERE version_id=? AND tenant_id=?",
            (version_id, tenant_id),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload"])
        if data.get("created_at"):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        return MediaAssetVersion(**data)

    def get_blob(self, version_id: str, *, tenant_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT blob FROM media_blobs WHERE version_id=? AND tenant_id=?",
            (version_id, tenant_id),
        ).fetchone()
        return None if row is None else row["blob"]

    def tombstone_version(self, version_id: str, *, tenant_id: str) -> bool:
        version = self.get_version(version_id, tenant_id=tenant_id)
        if version is None:
            return False
        updated = MediaAssetVersion(
            media_id=version.media_id,
            version_id=version.version_id,
            tenant_id=version.tenant_id,
            content_hash=version.content_hash,
            mime_type=version.mime_type,
            media_type=version.media_type,
            byte_size=version.byte_size,
            width=version.width,
            height=version.height,
            status=STATUS_TOMBSTONED,
            operation=version.operation,
            parent_version_id=version.parent_version_id,
            transform_profile=version.transform_profile,
            provider_id=version.provider_id,
            artifact_id=version.artifact_id,
            created_at=version.created_at,
            metadata_safe=dict(version.metadata_safe),
            rights_status=version.rights_status,
            source_content_hash=version.source_content_hash,
            recipe_id=version.recipe_id,
            recipe_version=version.recipe_version,
            target_profile_id=version.target_profile_id,
        )
        blob = self.get_blob(version_id, tenant_id=tenant_id) or b""
        self.save_version(updated, blob=blob)
        return True

    def save_link(self, link: ProductMediaLink) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO media_links(link_id, tenant_id, payload) VALUES (?,?,?)",
            (link.link_id, link.tenant_id, json.dumps(_jsonable(asdict(link)))),
        )
        self._conn.commit()

    def get_links(self, *, tenant_id: str, media_version_id: str) -> list[ProductMediaLink]:
        rows = self._conn.execute(
            "SELECT payload FROM media_links WHERE tenant_id=?",
            (tenant_id,),
        ).fetchall()
        out: list[ProductMediaLink] = []
        for row in rows:
            data = json.loads(row["payload"])
            if data.get("media_version_id") == media_version_id:
                out.append(ProductMediaLink(**data))
        return out

    def save_media_set(self, media_set: ProductMediaSet) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO media_sets(set_id, tenant_id, payload) VALUES (?,?,?)",
            (media_set.set_id, media_set.tenant_id, json.dumps(_jsonable(asdict(media_set)))),
        )
        self._conn.commit()

    def get_media_set(self, set_id: str, *, tenant_id: str) -> ProductMediaSet | None:
        row = self._conn.execute(
            "SELECT payload FROM media_sets WHERE set_id=? AND tenant_id=?",
            (set_id, tenant_id),
        ).fetchone()
        if row is None:
            return None
        return ProductMediaSet(**json.loads(row["payload"]))

    def save_job(self, job: MediaJob) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO media_jobs(job_id, tenant_id, payload) VALUES (?,?,?)",
            (job.job_id, job.tenant_id, json.dumps(_jsonable(asdict(job)))),
        )
        self._conn.commit()

    def get_job(self, job_id: str, *, tenant_id: str) -> MediaJob | None:
        row = self._conn.execute(
            "SELECT payload FROM media_jobs WHERE job_id=? AND tenant_id=?",
            (job_id, tenant_id),
        ).fetchone()
        if row is None:
            return None
        return MediaJob(**json.loads(row["payload"]))
