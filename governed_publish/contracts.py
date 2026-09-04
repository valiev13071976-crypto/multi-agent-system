"""Canonical governed publication/export contracts (Blocks 15–16)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from integrations.activation.models import ENV_FIXTURE, ENV_LIVE, ENV_SANDBOX

TARGET_SITE = "SITE"
TARGET_BITRIX_ASPRO = "BITRIX_ASPRO"
TARGET_WILDBERRIES = "WILDBERRIES"
TARGET_OZON = "OZON"
TARGET_YANDEX_MARKET = "YANDEX_MARKET"
TARGET_CUSTOM = "CUSTOM"

MODE_FIXTURE = ENV_FIXTURE
MODE_SANDBOX = ENV_SANDBOX
MODE_LIVE = ENV_LIVE

STATUS_PLANNED = "PLANNED"
STATUS_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
STATUS_APPROVED = "APPROVED"
STATUS_EXECUTED_FIXTURE = "EXECUTED_FIXTURE"
STATUS_REJECTED = "REJECTED"
STATUS_BLOCKED = "BLOCKED"
STATUS_FAILED = "FAILED"
STATUS_STALE = "STALE"
STATUS_ALREADY_EXECUTED = "ALREADY_EXECUTED"

DIFF_CREATE = "CREATE"
DIFF_UPDATE = "UPDATE"
DIFF_UNCHANGED = "UNCHANGED"
DIFF_REMOVE_REQUESTED = "REMOVE_REQUESTED"
DIFF_UNSUPPORTED = "UNSUPPORTED"
DIFF_BLOCKED = "BLOCKED"

MAP_MAPPED = "MAPPED"
MAP_AMBIGUOUS = "AMBIGUOUS"
MAP_MISSING = "MISSING"
MAP_UNSUPPORTED = "UNSUPPORTED"

COMP_ROLLBACK_SUPPORTED = "ROLLBACK_SUPPORTED"
COMP_COMPENSATION_REQUIRED = "COMPENSATION_REQUIRED"
COMP_MANUAL_RECOVERY = "MANUAL_RECOVERY"
COMP_UNSUPPORTED = "UNSUPPORTED"

POLICY_VERSION = "governed-publish-b-1.0.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def idempotency_key(*, tenant_id: str, product_id: str, content_version: str, target: str, action: str, policy: str) -> str:
    blob = "|".join([tenant_id, product_id, content_version, target, action, policy])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PublicationTarget:
    target_id: str
    target_type: str
    tenant_id: str
    store_ref: str = ""
    mode: str = MODE_FIXTURE
    capabilities: tuple[str, ...] = ()
    config_ref: str = ""
    mapping_policy: str = "default"
    publication_policy: str = POLICY_VERSION
    enabled: bool = True
    credential_ref: str = ""  # secret: prefix only — never plaintext

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("credential_ref", None)
        d["has_credential_ref"] = bool(self.credential_ref)
        return d


@dataclass(frozen=True)
class DiffEntry:
    field: str
    classification: str
    desired: Any = None
    current: Any = None
    omitted: bool = False


@dataclass(frozen=True)
class PublicationPreview:
    preview_id: str
    tenant_id: str
    target: str
    product_id: str
    content_version: str
    snapshot_version: str
    action: str
    fields_create: tuple[str, ...]
    fields_change: tuple[str, ...]
    fields_unchanged: tuple[str, ...]
    fields_omitted: tuple[str, ...]
    blocked_fields: tuple[str, ...]
    media_actions: tuple[str, ...]
    seo_actions: tuple[str, ...]
    warnings: tuple[str, ...]
    payload: dict[str, Any]
    desired: dict[str, Any]
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class PublicationPlan:
    plan_id: str
    tenant_id: str
    product_id: str
    sku: str
    article: str
    content_version: str
    target: str
    action: str
    mode: str
    status: str
    idempotency_key: str
    snapshot_version: str
    preview_id: str
    payload: dict[str, Any]
    warnings: tuple[str, ...]
    issues: tuple[str, ...]
    approval_id: str = ""
    policy_version: str = POLICY_VERSION
    compensation: str = COMP_MANUAL_RECOVERY
    published_live: bool = False
    created_at: str = ""


@dataclass(frozen=True)
class PublicationReceipt:
    receipt_id: str
    tenant_id: str
    target: str
    product_id: str
    content_version: str
    plan_id: str
    idempotency_key: str
    mode: str
    action: str
    status: str
    created_at: str
    approved_by: str
    audit_reference: str
    fixture_reference: str = ""
    warnings: tuple[str, ...] = ()
    compensation: str = COMP_MANUAL_RECOVERY
    published_live: bool = False


@dataclass(frozen=True)
class SelectionResult:
    selected: tuple[str, ...]
    excluded: tuple[str, ...]
    blocked: tuple[str, ...]
    ready: tuple[str, ...]
    review: tuple[str, ...]
    count: int
    mode: str
    inspectable: dict[str, Any]


def content_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
