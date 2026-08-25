"""VersionRegistry — explicit artifact version identity + content hash."""

from __future__ import annotations

from dataclasses import dataclass

from evals.models import ArtifactVersion


class VersionRegistryError(Exception):
    def __init__(self, error_code: str = "version_registry_error"):
        self.error_code = error_code
        super().__init__(error_code)


class VersionConflictError(VersionRegistryError):
    def __init__(self):
        super().__init__("version_content_hash_conflict")


class VersionNotFoundError(VersionRegistryError):
    def __init__(self):
        super().__init__("version_not_found")


@dataclass(frozen=True)
class _Key:
    artifact_type: str
    artifact_id: str
    version: str


class VersionRegistry:
    def __init__(self):
        self._items: dict[tuple[str, str, str], ArtifactVersion] = {}
        self._current: dict[tuple[str, str], str] = {}

    def register(
        self, artifact: ArtifactVersion, *, set_current: bool = True
    ) -> ArtifactVersion:
        key = (artifact.artifact_type, artifact.artifact_id, artifact.version)
        existing = self._items.get(key)
        if existing is not None:
            if existing.content_hash != artifact.content_hash:
                raise VersionConflictError()
            return existing
        self._items[key] = artifact
        if set_current:
            self._current[(artifact.artifact_type, artifact.artifact_id)] = (
                artifact.version
            )
        return artifact

    def get(
        self, artifact_type: str, artifact_id: str, version: str
    ) -> ArtifactVersion:
        row = self._items.get((artifact_type, artifact_id, version))
        if row is None:
            raise VersionNotFoundError()
        return row

    def list_versions(
        self, artifact_type: str, artifact_id: str
    ) -> tuple[ArtifactVersion, ...]:
        rows = [
            v
            for (t, i, _), v in self._items.items()
            if t == artifact_type and i == artifact_id
        ]
        return tuple(sorted(rows, key=lambda r: r.version))

    def current_version(self, artifact_type: str, artifact_id: str) -> str:
        ver = self._current.get((artifact_type, artifact_id))
        if ver is None:
            raise VersionNotFoundError()
        return ver

    def resolve(
        self, artifact_type: str, artifact_id: str, version: str | None = None
    ) -> ArtifactVersion:
        if version is None:
            version = self.current_version(artifact_type, artifact_id)
        return self.get(artifact_type, artifact_id, version)

    def compare(
        self,
        left: ArtifactVersion,
        right: ArtifactVersion,
    ) -> dict:
        return {
            "same_identity": (
                left.artifact_type == right.artifact_type
                and left.artifact_id == right.artifact_id
                and left.version == right.version
            ),
            "same_hash": left.content_hash == right.content_hash,
            "version_changed": left.version != right.version,
            "hash_changed": left.content_hash != right.content_hash,
        }
