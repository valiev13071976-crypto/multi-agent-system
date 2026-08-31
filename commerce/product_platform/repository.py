"""Product platform persistence — tenant-scoped SQLite tables."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from commerce.product_platform.errors import (
    COMMERCE_CROSS_TENANT,
    COMMERCE_IDENTIFIER_CONFLICT,
    COMMERCE_INSUFFICIENT_STOCK,
    COMMERCE_NOT_FOUND,
    COMMERCE_OVERSELL,
    ProductPlatformError,
)
from commerce.store import CommerceStore
from security.tenant import require_tenant_id

_PLATFORM_DDL = """
CREATE TABLE IF NOT EXISTS pp_products (
    tenant_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    current_version_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, product_id)
);
CREATE TABLE IF NOT EXISTS pp_product_versions (
    tenant_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, version_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_identifiers
ON pp_identifiers(tenant_id, identifier_type, identifier_value);
CREATE TABLE IF NOT EXISTS pp_identifiers (
    tenant_id TEXT NOT NULL,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    product_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, identifier_type, identifier_value)
);
CREATE TABLE IF NOT EXISTS pp_prices (
    tenant_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, product_id, currency)
);
CREATE TABLE IF NOT EXISTS pp_price_observations (
    tenant_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, observation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_obs_dedupe
ON pp_price_observations(tenant_id, content_hash);
CREATE TABLE IF NOT EXISTS pp_price_decisions (
    tenant_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, decision_id)
);
CREATE TABLE IF NOT EXISTS pp_inventory (
    tenant_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    on_hand TEXT NOT NULL,
    reserved TEXT NOT NULL,
    incoming TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, product_id, location_id)
);
CREATE TABLE IF NOT EXISTS pp_reservations (
    tenant_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    quantity TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, reservation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_res_idem
ON pp_reservations(tenant_id, idempotency_key);
CREATE TABLE IF NOT EXISTS pp_platform_orders (
    tenant_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, order_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_order_external
ON pp_platform_orders(tenant_id, external_ref);
CREATE TABLE IF NOT EXISTS pp_order_events (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id)
);
CREATE TABLE IF NOT EXISTS pp_cms_bindings (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, binding_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_cms_external
ON pp_cms_bindings(tenant_id, system, external_product_id);
CREATE TABLE IF NOT EXISTS pp_import_jobs (
    tenant_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, job_id)
);
CREATE TABLE IF NOT EXISTS pp_trusted_costs (
    tenant_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    PRIMARY KEY (tenant_id, product_id, currency)
);
"""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


class ProductPlatformRepository:
    def __init__(self, store: CommerceStore):
        self._store = store
        self._lock = store._lock
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        return self._store._connect()

    def _commit(self, conn) -> None:
        self._store._commit(conn)

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            # Fix DDL order - identifiers table must be created before index
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pp_products (
                    tenant_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    current_version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, product_id)
                );
                CREATE TABLE IF NOT EXISTS pp_product_versions (
                    tenant_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, version_id)
                );
                CREATE TABLE IF NOT EXISTS pp_identifiers (
                    tenant_id TEXT NOT NULL,
                    identifier_type TEXT NOT NULL,
                    identifier_value TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, identifier_type, identifier_value)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_identifiers
                ON pp_identifiers(tenant_id, identifier_type, identifier_value);
                CREATE TABLE IF NOT EXISTS pp_prices (
                    tenant_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, product_id, currency)
                );
                CREATE TABLE IF NOT EXISTS pp_price_observations (
                    tenant_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, observation_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_obs_dedupe
                ON pp_price_observations(tenant_id, content_hash);
                CREATE TABLE IF NOT EXISTS pp_price_decisions (
                    tenant_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, decision_id)
                );
                CREATE TABLE IF NOT EXISTS pp_inventory (
                    tenant_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    location_id TEXT NOT NULL,
                    on_hand TEXT NOT NULL,
                    reserved TEXT NOT NULL,
                    incoming TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, product_id, location_id)
                );
                CREATE TABLE IF NOT EXISTS pp_reservations (
                    tenant_id TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    location_id TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, reservation_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_res_idem
                ON pp_reservations(tenant_id, idempotency_key);
                CREATE TABLE IF NOT EXISTS pp_platform_orders (
                    tenant_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    external_ref TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (tenant_id, order_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_order_external
                ON pp_platform_orders(tenant_id, external_ref);
                CREATE TABLE IF NOT EXISTS pp_order_events (
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS pp_cms_bindings (
                    tenant_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    system TEXT NOT NULL DEFAULT '',
                    external_product_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (tenant_id, binding_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_cms_external
                ON pp_cms_bindings(tenant_id, system, external_product_id);
                CREATE TABLE IF NOT EXISTS pp_import_jobs (
                    tenant_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, job_id)
                );
                CREATE TABLE IF NOT EXISTS pp_trusted_costs (
                    tenant_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, product_id, currency)
                );
                CREATE TABLE IF NOT EXISTS pp_commerce_jobs (
                    tenant_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, job_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_order_event_external
                ON pp_order_events(tenant_id, order_id, event_id);
                """
            )
            self._commit(conn)

    def save_product_version(self, tenant_id: str, product_id: str, version_id: str, payload: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO pp_product_versions(tenant_id, version_id, product_id, payload_json, created_at) VALUES (?,?,?,?,?)",
                (tenant, version_id, product_id, _j(payload), _utc()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO pp_products(tenant_id, product_id, current_version_id, payload_json) VALUES (?,?,?,?)",
                (tenant, product_id, version_id, _j({"product_id": product_id, "current_version_id": version_id})),
            )
            self._commit(conn)

    def get_product_version(self, tenant_id: str, version_id: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM pp_product_versions WHERE tenant_id=? AND version_id=?",
            (tenant, version_id),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def get_product(self, tenant_id: str, product_id: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT current_version_id FROM pp_products WHERE tenant_id=? AND product_id=?",
            (tenant, product_id),
        ).fetchone()
        if row is None:
            return None
        return self.get_product_version(tenant, row["current_version_id"])

    def list_products(self, tenant_id: str) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT product_id, current_version_id FROM pp_products WHERE tenant_id=?",
            (tenant,),
        ).fetchall()
        out = []
        for row in rows:
            version = self.get_product_version(tenant, row["current_version_id"])
            if version:
                out.append(version)
        return out

    def bind_identifier(self, tenant_id: str, identifier_type: str, value: str, product_id: str) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            existing = conn.execute(
                "SELECT product_id FROM pp_identifiers WHERE tenant_id=? AND identifier_type=? AND identifier_value=?",
                (tenant, identifier_type, value),
            ).fetchone()
            if existing and existing["product_id"] != product_id:
                raise ProductPlatformError(COMMERCE_IDENTIFIER_CONFLICT)
            conn.execute(
                "INSERT OR REPLACE INTO pp_identifiers(tenant_id, identifier_type, identifier_value, product_id) VALUES (?,?,?,?)",
                (tenant, identifier_type, value, product_id),
            )
            self._commit(conn)

    def find_by_identifier(self, tenant_id: str, identifier_type: str, value: str) -> str | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT product_id FROM pp_identifiers WHERE tenant_id=? AND identifier_type=? AND identifier_value=?",
            (tenant, identifier_type, value),
        ).fetchone()
        return None if row is None else str(row["product_id"])

    def set_price(self, tenant_id: str, product_id: str, currency: str, amount: Decimal, *, expected_version: int | None = None) -> int:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version, amount FROM pp_prices WHERE tenant_id=? AND product_id=? AND currency=?",
                (tenant, product_id, currency),
            ).fetchone()
            if row is not None and expected_version is not None and int(row["version"]) != expected_version:
                conn.execute("ROLLBACK")
                from commerce.product_platform.errors import COMMERCE_PRICE_STALE_DECISION

                raise ProductPlatformError(COMMERCE_PRICE_STALE_DECISION)
            new_version = 1 if row is None else int(row["version"]) + 1
            conn.execute(
                "INSERT OR REPLACE INTO pp_prices(tenant_id, product_id, currency, amount, version, updated_at) VALUES (?,?,?,?,?,?)",
                (tenant, product_id, currency, str(amount), new_version, _utc()),
            )
            conn.execute("COMMIT")
            return new_version

    def get_price(self, tenant_id: str, product_id: str, currency: str) -> tuple[Decimal, int] | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT amount, version FROM pp_prices WHERE tenant_id=? AND product_id=? AND currency=?",
            (tenant, product_id, currency),
        ).fetchone()
        if row is None:
            return None
        return Decimal(row["amount"]), int(row["version"])

    def set_trusted_cost(self, tenant_id: str, product_id: str, currency: str, amount: Decimal) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO pp_trusted_costs(tenant_id, product_id, currency, amount) VALUES (?,?,?,?)",
                (tenant, product_id, currency, str(amount)),
            )
            self._commit(conn)

    def get_trusted_cost(self, tenant_id: str, product_id: str, currency: str) -> Decimal | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT amount FROM pp_trusted_costs WHERE tenant_id=? AND product_id=? AND currency=?",
            (tenant, product_id, currency),
        ).fetchone()
        return None if row is None else Decimal(row["amount"])

    def save_observation(self, tenant_id: str, observation_id: str, payload: dict, content_hash: str) -> bool:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            existing = conn.execute(
                "SELECT observation_id FROM pp_price_observations WHERE tenant_id=? AND content_hash=?",
                (tenant, content_hash),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                "INSERT INTO pp_price_observations(tenant_id, observation_id, payload_json, content_hash, observed_at) VALUES (?,?,?,?,?)",
                (tenant, observation_id, _j(payload), content_hash, payload.get("observed_at", _utc())),
            )
            self._commit(conn)
            return True

    def list_observations(self, tenant_id: str, product_id: str) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT payload_json FROM pp_price_observations WHERE tenant_id=?",
            (tenant,),
        ).fetchall()
        out = []
        for row in rows:
            data = json.loads(row["payload_json"])
            if data.get("product_id") == product_id:
                out.append(data)
        return out

    def save_decision(self, tenant_id: str, decision_id: str, payload: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO pp_price_decisions(tenant_id, decision_id, payload_json, applied) VALUES (?,?,?,?)",
                (tenant, decision_id, _j(payload), 0),
            )
            self._commit(conn)

    def get_decision(self, tenant_id: str, decision_id: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json, applied FROM pp_price_decisions WHERE tenant_id=? AND decision_id=?",
            (tenant, decision_id),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload_json"])
        data["applied"] = bool(row["applied"])
        return data

    def mark_decision_applied(self, tenant_id: str, decision_id: str) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "UPDATE pp_price_decisions SET applied=1 WHERE tenant_id=? AND decision_id=?",
                (tenant, decision_id),
            )
            self._commit(conn)

    def upsert_inventory(
        self,
        tenant_id: str,
        product_id: str,
        location_id: str,
        *,
        on_hand: Decimal,
        reserved: Decimal = Decimal("0"),
        incoming: Decimal = Decimal("0"),
        source: str = "manual",
    ) -> dict:
        from commerce.product_platform.models import InventoryPositionRecord

        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT version FROM pp_inventory WHERE tenant_id=? AND product_id=? AND location_id=?",
                (tenant, product_id, location_id),
            ).fetchone()
            version = 1 if row is None else int(row["version"]) + 1
            observed = _utc()
            conn.execute(
                "INSERT OR REPLACE INTO pp_inventory(tenant_id, product_id, location_id, on_hand, reserved, incoming, version, source, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant, product_id, location_id, str(on_hand), str(reserved), str(incoming), version, source, observed),
            )
            self._commit(conn)
        return InventoryPositionRecord(
            tenant_id=tenant,
            product_id=product_id,
            location_id=location_id,
            on_hand=on_hand,
            reserved=reserved,
            incoming=incoming,
            source=source,
            version=version,
            observed_at=datetime.fromisoformat(observed),
        )

    def get_inventory(self, tenant_id: str, product_id: str, location_id: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT on_hand, reserved, incoming, version, source, observed_at FROM pp_inventory WHERE tenant_id=? AND product_id=? AND location_id=?",
            (tenant, product_id, location_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "on_hand": Decimal(row["on_hand"]),
            "reserved": Decimal(row["reserved"]),
            "incoming": Decimal(row["incoming"]),
            "version": int(row["version"]),
            "source": row["source"],
            "observed_at": row["observed_at"],
        }

    def try_reserve(
        self,
        tenant_id: str,
        product_id: str,
        location_id: str,
        quantity: Decimal,
        *,
        reservation_id: str,
        idempotency_key: str,
    ) -> str:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            existing = conn.execute(
                "SELECT reservation_id, status FROM pp_reservations WHERE tenant_id=? AND idempotency_key=?",
                (tenant, idempotency_key),
            ).fetchone()
            if existing:
                return str(existing["reservation_id"])
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT on_hand, reserved, version FROM pp_inventory WHERE tenant_id=? AND product_id=? AND location_id=?",
                (tenant, product_id, location_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_INSUFFICIENT_STOCK)
            on_hand = Decimal(row["on_hand"])
            reserved = Decimal(row["reserved"])
            available = on_hand - reserved
            if available < quantity:
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_OVERSELL)
            new_reserved = reserved + quantity
            version = int(row["version"])
            updated = conn.execute(
                "UPDATE pp_inventory SET reserved=?, version=? WHERE tenant_id=? AND product_id=? AND location_id=? AND version=?",
                (str(new_reserved), version + 1, tenant, product_id, location_id, version),
            )
            if updated.rowcount != 1:
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_OVERSELL)
            conn.execute(
                "INSERT INTO pp_reservations(tenant_id, reservation_id, product_id, location_id, quantity, status, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (tenant, reservation_id, product_id, location_id, str(quantity), "active", idempotency_key, _utc()),
            )
            conn.execute("COMMIT")
            return reservation_id

    def release_reservation(self, tenant_id: str, reservation_id: str) -> bool:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT product_id, location_id, quantity, status FROM pp_reservations WHERE tenant_id=? AND reservation_id=?",
                (tenant, reservation_id),
            ).fetchone()
            if row is None or str(row["status"]) != "active":
                conn.execute("ROLLBACK")
                return False
            qty = Decimal(row["quantity"])
            product_id = str(row["product_id"])
            location_id = str(row["location_id"])
            inv = conn.execute(
                "SELECT reserved, version FROM pp_inventory WHERE tenant_id=? AND product_id=? AND location_id=?",
                (tenant, product_id, location_id),
            ).fetchone()
            if inv is None:
                conn.execute("ROLLBACK")
                return False
            new_reserved = max(Decimal(inv["reserved"]) - qty, Decimal("0"))
            version = int(inv["version"])
            conn.execute(
                "UPDATE pp_inventory SET reserved=?, version=? WHERE tenant_id=? AND product_id=? AND location_id=? AND version=?",
                (str(new_reserved), version + 1, tenant, product_id, location_id, version),
            )
            conn.execute(
                "UPDATE pp_reservations SET status=? WHERE tenant_id=? AND reservation_id=?",
                ("released", tenant, reservation_id),
            )
            conn.execute("COMMIT")
            return True

    def save_order(self, tenant_id: str, order_id: str, external_ref: str, payload: dict, status: str) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO pp_platform_orders(tenant_id, order_id, external_ref, payload_json, status, version) VALUES (?,?,?,?,?,?)",
                (tenant, order_id, external_ref, _j(payload), status, payload.get("version", 1)),
            )
            self._commit(conn)

    def get_order_by_external(self, tenant_id: str, external_ref: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM pp_platform_orders WHERE tenant_id=? AND external_ref=?",
            (tenant, external_ref),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def save_order_event(self, tenant_id: str, event_id: str, order_id: str, payload: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO pp_order_events(tenant_id, event_id, order_id, payload_json, created_at) VALUES (?,?,?,?,?)",
                (tenant, event_id, order_id, _j(payload), _utc()),
            )
            self._commit(conn)

    def save_cms_binding(self, tenant_id: str, binding_id: str, payload: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO pp_cms_bindings(tenant_id, binding_id, payload_json, system, external_product_id) VALUES (?,?,?,?,?)",
                (
                    tenant,
                    binding_id,
                    _j(payload),
                    payload.get("system", ""),
                    payload.get("external_product_id", ""),
                ),
            )
            self._commit(conn)

    def get_cms_binding(self, tenant_id: str, product_id: str, system: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        rows = self._conn().execute(
            "SELECT payload_json FROM pp_cms_bindings WHERE tenant_id=? AND system=?",
            (tenant, system),
        ).fetchall()
        for row in rows:
            data = json.loads(row["payload_json"])
            if data.get("product_id") == product_id:
                return data
        return None

    def save_import_job(self, tenant_id: str, job_id: str, payload: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO pp_import_jobs(tenant_id, job_id, payload_json) VALUES (?,?,?)",
                (tenant, job_id, _j(payload)),
            )
            self._commit(conn)

    def get_import_job(self, tenant_id: str, job_id: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM pp_import_jobs WHERE tenant_id=? AND job_id=?",
            (tenant, job_id),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def save_commerce_job(self, tenant_id: str, job_id: str, payload: dict) -> None:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO pp_commerce_jobs(tenant_id, job_id, payload_json) VALUES (?,?,?)",
                (tenant, job_id, _j(payload)),
            )
            self._commit(conn)

    def get_commerce_job(self, tenant_id: str, job_id: str) -> dict | None:
        tenant = require_tenant_id(tenant_id)
        row = self._conn().execute(
            "SELECT payload_json FROM pp_commerce_jobs WHERE tenant_id=? AND job_id=?",
            (tenant, job_id),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def try_insert_order_event(
        self,
        tenant_id: str,
        event_id: str,
        order_id: str,
        external_event_id: str,
        payload: dict,
    ) -> bool:
        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            if external_event_id:
                existing = conn.execute(
                    "SELECT event_id FROM pp_order_events WHERE tenant_id=? AND order_id=? AND json_extract(payload_json, '$.external_event_id')=?",
                    (tenant, order_id, external_event_id),
                ).fetchone()
                if existing:
                    return False
            try:
                conn.execute(
                    "INSERT INTO pp_order_events(tenant_id, event_id, order_id, payload_json, created_at) VALUES (?,?,?,?,?)",
                    (tenant, event_id, order_id, _j(payload), _utc()),
                )
                self._commit(conn)
                return True
            except sqlite3.IntegrityError:
                return False

    def transition_order_with_sequence(
        self,
        tenant_id: str,
        order_id: str,
        *,
        new_status: str,
        external_event_id: str = "",
        external_sequence: int | None = None,
        external_timestamp: str = "",
    ) -> dict:
        from commerce.product_platform.errors import (
            COMMERCE_NOT_FOUND,
            COMMERCE_ORDER_EVENT_CONFLICT,
            COMMERCE_ORDER_EVENT_IDEMPOTENT,
            COMMERCE_ORDER_STALE_EVENT,
            COMMERCE_ORDER_TRANSITION_INVALID,
        )

        tenant = require_tenant_id(tenant_id)
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_json, status FROM pp_platform_orders WHERE tenant_id=? AND order_id=?",
                (tenant, order_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_NOT_FOUND)
            payload = json.loads(row["payload_json"])
            current_status = row["status"]
            if external_event_id:
                dup = conn.execute(
                    "SELECT event_id FROM pp_order_events WHERE tenant_id=? AND order_id=? AND json_extract(payload_json, '$.external_event_id')=?",
                    (tenant, order_id, external_event_id),
                ).fetchone()
                if dup:
                    conn.execute("ROLLBACK")
                    raise ProductPlatformError(COMMERCE_ORDER_EVENT_IDEMPOTENT)
            last_seq = int(payload.get("last_external_event_sequence") or 0)
            last_ts = str(payload.get("last_external_event_timestamp") or "")
            if external_sequence is not None and external_sequence <= last_seq:
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_ORDER_STALE_EVENT)
            if external_timestamp and last_ts and external_timestamp < last_ts:
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_ORDER_STALE_EVENT)
            if external_sequence is not None and external_sequence == last_seq:
                if payload.get("status") != new_status:
                    conn.execute("ROLLBACK")
                    raise ProductPlatformError(COMMERCE_ORDER_EVENT_CONFLICT)
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_ORDER_EVENT_IDEMPOTENT)
            from commerce.product_platform.models import ORDER_TRANSITIONS

            allowed = ORDER_TRANSITIONS.get(current_status, set())
            if new_status not in allowed:
                conn.execute("ROLLBACK")
                raise ProductPlatformError(COMMERCE_ORDER_TRANSITION_INVALID)
            payload["status"] = new_status
            payload["version"] = int(payload.get("version", 1)) + 1
            if external_sequence is not None:
                payload["last_external_event_sequence"] = external_sequence
            if external_timestamp:
                payload["last_external_event_timestamp"] = external_timestamp
            if external_event_id:
                payload["last_external_event_id"] = external_event_id
            conn.execute(
                "INSERT OR REPLACE INTO pp_platform_orders(tenant_id, order_id, external_ref, payload_json, status, version) VALUES (?,?,?,?,?,?)",
                (
                    tenant,
                    order_id,
                    payload["external_ref"],
                    _j(payload),
                    new_status,
                    payload["version"],
                ),
            )
            conn.execute(
                "INSERT INTO pp_order_events(tenant_id, event_id, order_id, payload_json, created_at) VALUES (?,?,?,?,?)",
                (
                    tenant,
                    str(uuid.uuid4()),
                    order_id,
                    _j(
                        {
                            "prior_status": current_status,
                            "new_status": new_status,
                            "external_event_id": external_event_id,
                            "external_sequence": external_sequence,
                        }
                    ),
                    _utc(),
                ),
            )
            conn.execute("COMMIT")
            return {"order_id": order_id, "status": new_status, "version": payload["version"]}
