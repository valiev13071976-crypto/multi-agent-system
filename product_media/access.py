"""Safe filename and tenant access helpers."""

from __future__ import annotations

import re

from product_media.errors import MEDIA_ACCESS_DENIED, MEDIA_CROSS_TENANT, MEDIA_DELETED, MediaError
from security.tenant import require_tenant_id

_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f]|(\.\.)|[<>\"'`|;&$]")


def sanitize_filename(name: str) -> str:
    raw = str(name or "media.bin").replace("\\", "/")
    if ".." in raw or raw.startswith("/"):
        return "media.bin"
    raw = raw.rsplit("/", 1)[-1]
    if not raw or _UNSAFE_FILENAME.search(raw):
        return "media.bin"
    return raw[:255]


def assert_tenant_match(*, trusted: str, payload: str | None) -> str:
    tenant = require_tenant_id(trusted)
    if payload is not None and require_tenant_id(payload) != tenant:
        raise MediaError(MEDIA_CROSS_TENANT)
    return tenant


def assert_version_access(version, *, tenant_id: str):
    tenant = require_tenant_id(tenant_id)
    if version is None:
        raise MediaError(MEDIA_ACCESS_DENIED)
    if require_tenant_id(version.tenant_id) != tenant:
        raise MediaError(MEDIA_CROSS_TENANT)
    if version.status in ("tombstoned", "deleted"):
        raise MediaError(MEDIA_DELETED, "media deleted")
    return version
