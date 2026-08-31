"""Configurable field ownership for Panda ↔ Bitrix ↔ 1C sync."""

from __future__ import annotations

import uuid

from commerce.product_platform.errors import COMMERCE_FIELD_OWNERSHIP_REQUIRED, COMMERCE_SYNC_CONFLICT, ProductPlatformError
from commerce.product_platform.models import (
    MODE_AUTHORITATIVE,
    MODE_PROPOSE_ONLY,
    MODE_READ_ONLY,
    MODE_WRITE_ALLOWED,
    OWNER_BITRIX,
    OWNER_ONE_C,
    OWNER_PANDA,
    OWNER_SUPPLIER,
    OWNERSHIP_POLICY_VERSION,
    FieldOwnershipPolicy,
    FieldOwnershipRule,
    SyncConflict,
)


def default_ownership_policy(*, tenant_id: str) -> FieldOwnershipPolicy:
    rules = (
        FieldOwnershipRule(field="stock", owner=OWNER_ONE_C, mode=MODE_AUTHORITATIVE, integration="1c"),
        FieldOwnershipRule(field="purchase_price", owner=OWNER_ONE_C, mode=MODE_AUTHORITATIVE, integration="1c"),
        FieldOwnershipRule(field="retail_price", owner=OWNER_PANDA, mode=MODE_AUTHORITATIVE, integration="panda"),
        FieldOwnershipRule(field="description", owner=OWNER_PANDA, mode=MODE_AUTHORITATIVE, integration="panda"),
        FieldOwnershipRule(field="publication_status", owner=OWNER_BITRIX, mode=MODE_AUTHORITATIVE, integration="bitrix"),
        FieldOwnershipRule(field="title", owner=OWNER_SUPPLIER, mode=MODE_PROPOSE_ONLY, integration="supplier"),
        FieldOwnershipRule(field="sku", owner=OWNER_PANDA, mode=MODE_READ_ONLY, integration="panda"),
    )
    return FieldOwnershipPolicy(
        policy_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        version=OWNERSHIP_POLICY_VERSION,
        rules=rules,
    )


def rule_for(policy: FieldOwnershipPolicy, field: str) -> FieldOwnershipRule | None:
    for rule in policy.rules:
        if rule.field == field:
            return rule
    return None


def assert_write_allowed(policy: FieldOwnershipPolicy, *, field: str, actor: str) -> None:
    rule = rule_for(policy, field)
    if rule is None:
        raise ProductPlatformError(COMMERCE_FIELD_OWNERSHIP_REQUIRED, field)
    if rule.mode == MODE_READ_ONLY:
        raise ProductPlatformError(COMMERCE_SYNC_CONFLICT, f"read_only:{field}")
    if rule.mode == MODE_AUTHORITATIVE and rule.owner != actor:
        raise ProductPlatformError(COMMERCE_SYNC_CONFLICT, f"owner:{rule.owner}:actor:{actor}")
    if rule.mode == MODE_WRITE_ALLOWED and actor not in {rule.owner, OWNER_PANDA}:
        raise ProductPlatformError(COMMERCE_SYNC_CONFLICT, f"write_denied:{field}")


def detect_sync_conflict(
    *,
    tenant_id: str,
    entity_type: str,
    entity_id: str,
    field: str,
    canonical_value: str,
    external_value: str,
    policy: FieldOwnershipPolicy,
) -> SyncConflict | None:
    if canonical_value == external_value:
        return None
    rule = rule_for(policy, field)
    ownership = rule.owner if rule else "UNKNOWN"
    return SyncConflict(
        conflict_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        canonical_value=canonical_value,
        external_value=external_value,
        ownership=ownership,
        resolution_status="OPEN",
    )
