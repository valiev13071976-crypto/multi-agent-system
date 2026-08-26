"""SQLite procurement store — shared or dedicated connection ownership."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from autonomy.models import sanitize_metadata
from memory.models import MemoryScope
from procurement.errors import PROCUREMENT_SCOPE_DENIED, ProcurementError
from procurement.models import (
    PROCUREMENT_SCHEMA_VERSION,
    ComparisonRow,
    Money,
    OfferProvenance,
    ProcurementRecommendation,
    ProcurementRequest,
    ProcurementRequirement,
    RiskFinding,
    Supplier,
    SupplierOffer,
    parse_money_amount,
)
from security.encryption import (
    ENCRYPTION_REQUIRED,
    SENSITIVITY_INTERNAL,
    EncryptionService,
    EncryptionUnavailableError,
)


DDL = f"""
CREATE TABLE IF NOT EXISTS procurement_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS procurement_requests (
    request_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    item_name TEXT NOT NULL,
    description TEXT,
    quantity TEXT,
    unit TEXT,
    target_budget_amount TEXT,
    target_budget_currency TEXT,
    currency TEXT,
    required_by TEXT,
    delivery_location TEXT,
    specifications_json TEXT NOT NULL DEFAULT '{{}}',
    preferred_suppliers_json TEXT NOT NULL DEFAULT '[]',
    excluded_suppliers_json TEXT NOT NULL DEFAULT '[]',
    constraints_json TEXT NOT NULL DEFAULT '{{}}',
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_procurement_requests_scope
ON procurement_requests(scope_type, scope_id);
CREATE TABLE IF NOT EXISTS procurement_requirements (
    request_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS procurement_suppliers (
    supplier_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    country TEXT,
    website_ref TEXT,
    contact_ref TEXT,
    categories_json TEXT NOT NULL DEFAULT '[]',
    trust_level TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{{}}',
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_procurement_suppliers_scope
ON procurement_suppliers(scope_type, scope_id);
CREATE TABLE IF NOT EXISTS procurement_offers (
    offer_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    currency TEXT NOT NULL,
    quantity TEXT,
    unit_price_amount TEXT,
    unit_price_currency TEXT,
    subtotal_amount TEXT,
    subtotal_currency TEXT,
    shipping_amount TEXT,
    shipping_currency TEXT,
    tax_amount TEXT,
    tax_currency TEXT,
    total_amount TEXT,
    total_currency TEXT,
    lead_time_days INTEGER,
    minimum_order_quantity TEXT,
    payment_terms TEXT,
    delivery_terms TEXT,
    valid_until TEXT,
    availability TEXT,
    warranty TEXT,
    specifications_json TEXT NOT NULL DEFAULT '{{}}',
    compliance_json TEXT NOT NULL DEFAULT '{{}}',
    provenance_json TEXT NOT NULL,
    confidence TEXT,
    status TEXT NOT NULL,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    commercial_safe_json TEXT,
    commercial_encrypted TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_procurement_offers_request_supplier_source
ON procurement_offers(request_id, supplier_id, source_ref);
CREATE INDEX IF NOT EXISTS idx_procurement_offers_scope
ON procurement_offers(scope_type, scope_id, request_id);
CREATE TABLE IF NOT EXISTS procurement_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    recommended_supplier_id TEXT,
    recommended_offer_id TEXT,
    alternatives_json TEXT NOT NULL DEFAULT '[]',
    reasoning_summary TEXT NOT NULL,
    comparison_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    missing_information_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    requires_approval INTEGER NOT NULL,
    status TEXT NOT NULL,
    single_source_procurement INTEGER NOT NULL,
    currency_conversion_required INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_procurement_rec_request
ON procurement_recommendations(request_id);
CREATE INDEX IF NOT EXISTS idx_procurement_rec_scope
ON procurement_recommendations(scope_type, scope_id);
"""


class ProcurementPersistenceUnavailableError(ProcurementError):
    def __init__(self, reason: str = "procurement_store_unavailable"):
        super().__init__(reason)


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_db(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _json_dumps(value) -> str:
    if isinstance(value, (list, tuple)):
        cleaned = []
        for item in value:
            if isinstance(item, dict):
                cleaned.append(sanitize_metadata(item))
            else:
                cleaned.append(item)
        return json.dumps(cleaned, separators=(",", ":"), sort_keys=True, default=str)
    return json.dumps(sanitize_metadata(value or {}), separators=(",", ":"), sort_keys=True, default=str)


def _json_loads(raw: str | None, default=None):
    if not raw:
        return default if default is not None else {}
    return json.loads(raw)


def _dec_to_db(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)), "f")


def _dec_from_db(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return parse_money_amount(value)


def _money_to_db(money: Money | None) -> tuple[str | None, str | None]:
    if money is None:
        return None, None
    return _dec_to_db(money.amount), money.currency


def _money_from_db(amount: str | None, currency: str | None) -> Money | None:
    if amount is None or not currency:
        return None
    return Money(amount=parse_money_amount(amount), currency=str(currency))


class SqliteProcurementStore:
    """Canonical durable store implementing request/supplier/offer/recommendation APIs."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        shared_connection=None,
        owns_connection: bool | None = None,
        encryption: EncryptionService | None = None,
    ):
        self._lock = threading.RLock()
        self._local = threading.local()
        self.available = True
        self._shared = shared_connection
        self.encryption = encryption
        self._requirements_cache: dict[str, object] = {}
        if shared_connection is not None:
            self.path = Path(getattr(shared_connection, "path", ".") or ".")
            self.owns_connection = False if owns_connection is None else bool(owns_connection)
            self.connection_mode = "shared"
            self.persistence_backend = "sqlite"
        elif db_path is not None and str(db_path).strip():
            self.path = Path(str(db_path).strip())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.owns_connection = True if owns_connection is None else bool(owns_connection)
            self.connection_mode = "dedicated"
            self.persistence_backend = "sqlite"
        else:
            raise ValueError("procurement_store_requires_path_or_shared_connection")
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared.connect()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _commit(self, conn: sqlite3.Connection) -> None:
        if self._shared is not None:
            self._shared.maybe_autocommit()
            return
        conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(DDL)
                row = conn.execute(
                    "SELECT value FROM procurement_schema_meta WHERE key='schema_version'"
                ).fetchone()
                if row is not None and int(row["value"]) > PROCUREMENT_SCHEMA_VERSION:
                    self.available = False
                    raise ProcurementPersistenceUnavailableError(
                        "procurement_schema_version_unsupported"
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO procurement_schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(PROCUREMENT_SCHEMA_VERSION)),
                )
                self._commit(conn)
            except ProcurementPersistenceUnavailableError:
                raise
            except Exception as exc:
                self.available = False
                raise ProcurementPersistenceUnavailableError(
                    "procurement_store_unavailable"
                ) from exc

    def close(self) -> None:
        if not self.owns_connection:
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # --- requests ---
    def save_request(self, request: ProcurementRequest) -> ProcurementRequest:
        with self._lock:
            conn = self._connect()
            budget_a, budget_c = _money_to_db(request.target_budget)
            conn.execute(
                """
                INSERT INTO procurement_requests(
                    request_id, scope_type, scope_id, requested_by, item_name, description,
                    quantity, unit, target_budget_amount, target_budget_currency, currency,
                    required_by, delivery_location, specifications_json, preferred_suppliers_json,
                    excluded_suppliers_json, constraints_json, metadata_json, status,
                    created_at, updated_at, version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(request_id) DO UPDATE SET
                    scope_type=excluded.scope_type,
                    scope_id=excluded.scope_id,
                    requested_by=excluded.requested_by,
                    item_name=excluded.item_name,
                    description=excluded.description,
                    quantity=excluded.quantity,
                    unit=excluded.unit,
                    target_budget_amount=excluded.target_budget_amount,
                    target_budget_currency=excluded.target_budget_currency,
                    currency=excluded.currency,
                    required_by=excluded.required_by,
                    delivery_location=excluded.delivery_location,
                    specifications_json=excluded.specifications_json,
                    preferred_suppliers_json=excluded.preferred_suppliers_json,
                    excluded_suppliers_json=excluded.excluded_suppliers_json,
                    constraints_json=excluded.constraints_json,
                    metadata_json=excluded.metadata_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    version=excluded.version
                """,
                (
                    request.request_id,
                    request.scope.scope_type,
                    request.scope.scope_id,
                    request.requested_by,
                    request.item_name,
                    request.description,
                    _dec_to_db(request.quantity),
                    request.unit,
                    budget_a,
                    budget_c,
                    request.currency,
                    _dt_to_db(request.required_by),
                    request.delivery_location,
                    _json_dumps(dict(request.specifications)),
                    _json_dumps(list(request.preferred_suppliers)),
                    _json_dumps(list(request.excluded_suppliers)),
                    _json_dumps(dict(request.constraints)),
                    _json_dumps(dict(request.metadata_safe)),
                    request.status,
                    _dt_to_db(request.created_at),
                    _dt_to_db(request.updated_at),
                    int(request.version),
                ),
            )
            self._commit(conn)
            return request

    def get_request(self, request_id: str, *, requesting_scope) -> ProcurementRequest | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM procurement_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            scope = MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"])
            if scope.key() != requesting_scope.key():
                raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
            return self._row_to_request(row)

    def save_requirement(self, request_id: str, requirement) -> None:
        self._requirements_cache[request_id] = requirement
        payload = self._requirement_to_dict(requirement)
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO procurement_requirements(request_id, payload_json)
                VALUES (?,?)
                ON CONFLICT(request_id) DO UPDATE SET payload_json=excluded.payload_json
                """,
                (request_id, _json_dumps(payload)),
            )
            self._commit(conn)

    def get_requirement(self, request_id: str):
        if request_id in self._requirements_cache:
            return self._requirements_cache[request_id]
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json FROM procurement_requirements WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            req = self._dict_to_requirement(_json_loads(row["payload_json"], {}))
            self._requirements_cache[request_id] = req
            return req

    def save_recommendation(self, rec: ProcurementRecommendation) -> ProcurementRecommendation:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO procurement_recommendations(
                    recommendation_id, request_id, scope_type, scope_id,
                    recommended_supplier_id, recommended_offer_id, alternatives_json,
                    reasoning_summary, comparison_json, risks_json, assumptions_json,
                    missing_information_json, confidence, citations_json, requires_approval,
                    status, single_source_procurement, currency_conversion_required,
                    metadata_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(recommendation_id) DO UPDATE SET
                    request_id=excluded.request_id,
                    scope_type=excluded.scope_type,
                    scope_id=excluded.scope_id,
                    recommended_supplier_id=excluded.recommended_supplier_id,
                    recommended_offer_id=excluded.recommended_offer_id,
                    alternatives_json=excluded.alternatives_json,
                    reasoning_summary=excluded.reasoning_summary,
                    comparison_json=excluded.comparison_json,
                    risks_json=excluded.risks_json,
                    assumptions_json=excluded.assumptions_json,
                    missing_information_json=excluded.missing_information_json,
                    confidence=excluded.confidence,
                    citations_json=excluded.citations_json,
                    requires_approval=excluded.requires_approval,
                    status=excluded.status,
                    single_source_procurement=excluded.single_source_procurement,
                    currency_conversion_required=excluded.currency_conversion_required,
                    metadata_json=excluded.metadata_json,
                    created_at=excluded.created_at
                """,
                (
                    rec.recommendation_id,
                    rec.request_id,
                    rec.scope.scope_type,
                    rec.scope.scope_id,
                    rec.recommended_supplier_id,
                    rec.recommended_offer_id,
                    _json_dumps(list(rec.alternatives)),
                    rec.reasoning_summary,
                    _json_dumps([self._comparison_to_dict(c) for c in rec.comparison]),
                    _json_dumps([self._risk_to_dict(r) for r in rec.risks]),
                    _json_dumps(list(rec.assumptions)),
                    _json_dumps(list(rec.missing_information)),
                    format(Decimal(str(rec.confidence)), "f"),
                    _json_dumps(list(rec.citations)),
                    1 if rec.requires_approval else 0,
                    rec.status,
                    1 if rec.single_source_procurement else 0,
                    1 if rec.currency_conversion_required else 0,
                    _json_dumps(dict(rec.metadata_safe)),
                    _dt_to_db(rec.created_at),
                ),
            )
            # Ensure one active recommendation per request
            conn.execute(
                "DELETE FROM procurement_recommendations WHERE request_id=? AND recommendation_id<>?",
                (rec.request_id, rec.recommendation_id),
            )
            self._commit(conn)
            return rec

    def get_recommendation_for_request(
        self, request_id: str, *, requesting_scope
    ) -> ProcurementRecommendation | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM procurement_recommendations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            scope = MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"])
            if scope.key() != requesting_scope.key():
                raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
            return self._row_to_recommendation(row)

    # --- suppliers ---
    def upsert(self, entity):
        """Dispatch upsert for Supplier or SupplierOffer based on type."""
        if isinstance(entity, Supplier):
            return self._upsert_supplier(entity)
        if isinstance(entity, SupplierOffer):
            return self._upsert_offer(entity)
        raise TypeError(type(entity))

    def _upsert_supplier(self, supplier: Supplier) -> Supplier:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO procurement_suppliers(
                    supplier_id, scope_type, scope_id, name, source, source_ref, country,
                    website_ref, contact_ref, categories_json, trust_level, status,
                    risk_flags_json, provenance_json, metadata_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(supplier_id) DO UPDATE SET
                    scope_type=excluded.scope_type,
                    scope_id=excluded.scope_id,
                    name=excluded.name,
                    source=excluded.source,
                    source_ref=excluded.source_ref,
                    country=excluded.country,
                    website_ref=excluded.website_ref,
                    contact_ref=excluded.contact_ref,
                    categories_json=excluded.categories_json,
                    trust_level=excluded.trust_level,
                    status=excluded.status,
                    risk_flags_json=excluded.risk_flags_json,
                    provenance_json=excluded.provenance_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    supplier.supplier_id,
                    supplier.scope.scope_type,
                    supplier.scope.scope_id,
                    supplier.name,
                    supplier.source,
                    supplier.source_ref,
                    supplier.country,
                    supplier.website_ref,
                    supplier.contact_ref,
                    _json_dumps(list(supplier.categories)),
                    supplier.trust_level,
                    supplier.status,
                    _json_dumps(list(supplier.risk_flags)),
                    _json_dumps(dict(supplier.provenance)),
                    _json_dumps(dict(supplier.metadata_safe)),
                    _dt_to_db(supplier.created_at),
                    _dt_to_db(supplier.updated_at),
                ),
            )
            self._commit(conn)
            return supplier

    def get(self, entity_id: str, *, requesting_scope):
        """Get supplier by id (supplier repo API) or offer by id when used as offer repo.

        Disambiguated by table lookup order: offer first if caller is offer path —
        we expose get_supplier/get_offer explicitly and route get via dual lookup.
        """
        offer = self._get_offer(entity_id, requesting_scope=requesting_scope, soft=True)
        if offer is not None:
            return offer
        return self._get_supplier(entity_id, requesting_scope=requesting_scope)

    def _get_supplier(self, supplier_id: str, *, requesting_scope) -> Supplier | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM procurement_suppliers WHERE supplier_id=?",
                (supplier_id,),
            ).fetchone()
            if row is None:
                return None
            scope = MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"])
            if scope.key() != requesting_scope.key():
                raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
            return self._row_to_supplier(row)

    def list_for_scope(self, scope) -> tuple[Supplier, ...]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM procurement_suppliers WHERE scope_type=? AND scope_id=? ORDER BY supplier_id",
                (scope.scope_type, scope.scope_id),
            ).fetchall()
            return tuple(self._row_to_supplier(r) for r in rows)

    def find(
        self,
        *,
        scope,
        categories: tuple[str, ...] = (),
        statuses: tuple[str, ...] = (),
        exclude_restricted: bool = False,
    ) -> tuple[Supplier, ...]:
        rows = self.list_for_scope(scope)
        if categories:
            cats = {c.lower() for c in categories}
            rows = tuple(
                s
                for s in rows
                if cats.intersection({c.lower() for c in s.categories}) or not s.categories
            )
        if statuses:
            wanted = set(statuses)
            rows = tuple(s for s in rows if s.status in wanted)
        if exclude_restricted:
            rows = tuple(s for s in rows if s.status != "restricted")
        return rows

    # --- offers ---
    def _upsert_offer(self, offer: SupplierOffer) -> SupplierOffer:
        sensitivity = str(dict(offer.metadata_safe or {}).get("sensitivity") or SENSITIVITY_INTERNAL)
        commercial = {
            "unit_price": offer.unit_price.as_dict() if offer.unit_price else None,
            "subtotal": offer.subtotal.as_dict() if offer.subtotal else None,
            "shipping_cost": offer.shipping_cost.as_dict() if offer.shipping_cost else None,
            "tax": offer.tax.as_dict() if offer.tax else None,
            "total_cost": offer.total_cost.as_dict() if offer.total_cost else None,
        }
        commercial_safe = None
        commercial_encrypted = None
        if sensitivity in ENCRYPTION_REQUIRED:
            if self.encryption is None:
                raise EncryptionUnavailableError("encryption_required_for_sensitive_offer")
            commercial_encrypted = self.encryption.encrypt(
                json.dumps(commercial, separators=(",", ":"), sort_keys=True)
            ).serialize()
            unit_a = unit_c = sub_a = sub_c = ship_a = ship_c = tax_a = tax_c = tot_a = tot_c = None
        else:
            commercial_safe = _json_dumps(commercial)
            unit_a, unit_c = _money_to_db(offer.unit_price)
            sub_a, sub_c = _money_to_db(offer.subtotal)
            ship_a, ship_c = _money_to_db(offer.shipping_cost)
            tax_a, tax_c = _money_to_db(offer.tax)
            tot_a, tot_c = _money_to_db(offer.total_cost)

        prov = {
            "source_id": offer.provenance.source_id,
            "source_ref": offer.provenance.source_ref,
            "retrieved_at": _dt_to_db(offer.provenance.retrieved_at),
            "content_hash": offer.provenance.content_hash,
            "trust": offer.provenance.trust,
            "freshness": offer.provenance.freshness,
            "document_id": offer.provenance.document_id,
            "chunk_id": offer.provenance.chunk_id,
        }
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO procurement_offers(
                    offer_id, request_id, supplier_id, scope_type, scope_id, source_type, source_ref,
                    currency, quantity, unit_price_amount, unit_price_currency, subtotal_amount,
                    subtotal_currency, shipping_amount, shipping_currency, tax_amount, tax_currency,
                    total_amount, total_currency, lead_time_days, minimum_order_quantity,
                    payment_terms, delivery_terms, valid_until, availability, warranty,
                    specifications_json, compliance_json, provenance_json, confidence, status,
                    sensitivity, commercial_safe_json, commercial_encrypted, metadata_json,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(offer_id) DO UPDATE SET
                    request_id=excluded.request_id,
                    supplier_id=excluded.supplier_id,
                    scope_type=excluded.scope_type,
                    scope_id=excluded.scope_id,
                    source_type=excluded.source_type,
                    source_ref=excluded.source_ref,
                    currency=excluded.currency,
                    quantity=excluded.quantity,
                    unit_price_amount=excluded.unit_price_amount,
                    unit_price_currency=excluded.unit_price_currency,
                    subtotal_amount=excluded.subtotal_amount,
                    subtotal_currency=excluded.subtotal_currency,
                    shipping_amount=excluded.shipping_amount,
                    shipping_currency=excluded.shipping_currency,
                    tax_amount=excluded.tax_amount,
                    tax_currency=excluded.tax_currency,
                    total_amount=excluded.total_amount,
                    total_currency=excluded.total_currency,
                    lead_time_days=excluded.lead_time_days,
                    minimum_order_quantity=excluded.minimum_order_quantity,
                    payment_terms=excluded.payment_terms,
                    delivery_terms=excluded.delivery_terms,
                    valid_until=excluded.valid_until,
                    availability=excluded.availability,
                    warranty=excluded.warranty,
                    specifications_json=excluded.specifications_json,
                    compliance_json=excluded.compliance_json,
                    provenance_json=excluded.provenance_json,
                    confidence=excluded.confidence,
                    status=excluded.status,
                    sensitivity=excluded.sensitivity,
                    commercial_safe_json=excluded.commercial_safe_json,
                    commercial_encrypted=excluded.commercial_encrypted,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    offer.offer_id,
                    offer.request_id,
                    offer.supplier_id,
                    offer.scope.scope_type,
                    offer.scope.scope_id,
                    offer.source_type,
                    offer.source_ref,
                    offer.currency,
                    _dec_to_db(offer.quantity),
                    unit_a,
                    unit_c,
                    sub_a,
                    sub_c,
                    ship_a,
                    ship_c,
                    tax_a,
                    tax_c,
                    tot_a,
                    tot_c,
                    offer.lead_time_days,
                    _dec_to_db(offer.minimum_order_quantity),
                    offer.payment_terms,
                    offer.delivery_terms,
                    _dt_to_db(offer.valid_until),
                    offer.availability,
                    offer.warranty,
                    _json_dumps(dict(offer.specifications)),
                    _json_dumps(dict(offer.compliance)),
                    _json_dumps(prov),
                    format(Decimal(str(offer.confidence)), "f") if offer.confidence is not None else None,
                    offer.status,
                    sensitivity,
                    commercial_safe,
                    commercial_encrypted,
                    _json_dumps(dict(offer.metadata_safe)),
                    _dt_to_db(offer.created_at),
                    _dt_to_db(offer.updated_at),
                ),
            )
            self._commit(conn)
            return offer

    def _get_offer(self, offer_id: str, *, requesting_scope, soft: bool = False) -> SupplierOffer | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM procurement_offers WHERE offer_id=?",
                (offer_id,),
            ).fetchone()
            if row is None:
                return None
            scope = MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"])
            if scope.key() != requesting_scope.key():
                if soft:
                    return None
                raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
            return self._row_to_offer(row)

    def list_for_request(self, request_id: str, *, scope) -> tuple[SupplierOffer, ...]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT * FROM procurement_offers
                WHERE request_id=? AND scope_type=? AND scope_id=?
                ORDER BY offer_id
                """,
                (request_id, scope.scope_type, scope.scope_id),
            ).fetchall()
            return tuple(self._row_to_offer(r) for r in rows)

    # --- row mappers ---
    def _row_to_request(self, row) -> ProcurementRequest:
        return ProcurementRequest(
            request_id=row["request_id"],
            scope=MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"]),
            requested_by=row["requested_by"],
            item_name=row["item_name"],
            quantity=_dec_from_db(row["quantity"]),
            unit=row["unit"],
            specifications=_json_loads(row["specifications_json"], {}),
            description=row["description"],
            target_budget=_money_from_db(row["target_budget_amount"], row["target_budget_currency"]),
            currency=row["currency"],
            required_by=_dt_from_db(row["required_by"]),
            delivery_location=row["delivery_location"],
            preferred_suppliers=tuple(_json_loads(row["preferred_suppliers_json"], [])),
            excluded_suppliers=tuple(_json_loads(row["excluded_suppliers_json"], [])),
            constraints=_json_loads(row["constraints_json"], {}),
            metadata_safe=_json_loads(row["metadata_json"], {}),
            status=row["status"],
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
            version=int(row["version"]),
        )

    def _row_to_supplier(self, row) -> Supplier:
        return Supplier(
            supplier_id=row["supplier_id"],
            scope=MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"]),
            name=row["name"],
            source=row["source"],
            source_ref=row["source_ref"],
            categories=tuple(_json_loads(row["categories_json"], [])),
            trust_level=row["trust_level"],
            status=row["status"],
            risk_flags=tuple(_json_loads(row["risk_flags_json"], [])),
            provenance=_json_loads(row["provenance_json"], {}),
            country=row["country"],
            website_ref=row["website_ref"],
            contact_ref=row["contact_ref"],
            metadata_safe=_json_loads(row["metadata_json"], {}),
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
        )

    def _row_to_offer(self, row) -> SupplierOffer:
        commercial = {}
        if row["commercial_encrypted"]:
            if self.encryption is None:
                raise EncryptionUnavailableError("encryption_required_to_read_sensitive_offer")
            commercial = json.loads(self.encryption.decrypt(row["commercial_encrypted"]))
        elif row["commercial_safe_json"]:
            commercial = _json_loads(row["commercial_safe_json"], {})

        def _m(key: str, amount_col: str, currency_col: str) -> Money | None:
            blob = commercial.get(key) if commercial else None
            if isinstance(blob, dict) and blob.get("amount") is not None:
                return Money(amount=parse_money_amount(blob["amount"]), currency=blob["currency"])
            return _money_from_db(row[amount_col], row[currency_col])

        prov_raw = _json_loads(row["provenance_json"], {})
        provenance = OfferProvenance(
            source_id=prov_raw["source_id"],
            source_ref=prov_raw["source_ref"],
            retrieved_at=_dt_from_db(prov_raw.get("retrieved_at")),
            content_hash=prov_raw["content_hash"],
            trust=prov_raw.get("trust", "unverified_external"),
            freshness=prov_raw.get("freshness", "unknown"),
            document_id=prov_raw.get("document_id"),
            chunk_id=prov_raw.get("chunk_id"),
        )
        conf = row["confidence"]
        return SupplierOffer(
            offer_id=row["offer_id"],
            request_id=row["request_id"],
            supplier_id=row["supplier_id"],
            scope=MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"]),
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            currency=row["currency"],
            unit_price=_m("unit_price", "unit_price_amount", "unit_price_currency"),
            quantity=_dec_from_db(row["quantity"]),
            provenance=provenance,
            subtotal=_m("subtotal", "subtotal_amount", "subtotal_currency"),
            shipping_cost=_m("shipping_cost", "shipping_amount", "shipping_currency"),
            tax=_m("tax", "tax_amount", "tax_currency"),
            total_cost=_m("total_cost", "total_amount", "total_currency"),
            lead_time_days=row["lead_time_days"],
            minimum_order_quantity=_dec_from_db(row["minimum_order_quantity"]),
            payment_terms=row["payment_terms"],
            delivery_terms=row["delivery_terms"],
            valid_until=_dt_from_db(row["valid_until"]),
            availability=row["availability"],
            warranty=row["warranty"],
            specifications=_json_loads(row["specifications_json"], {}),
            compliance=_json_loads(row["compliance_json"], {}),
            confidence=float(conf) if conf is not None else None,
            status=row["status"],
            metadata_safe=_json_loads(row["metadata_json"], {}),
            created_at=_dt_from_db(row["created_at"]),
            updated_at=_dt_from_db(row["updated_at"]),
        )

    def _row_to_recommendation(self, row) -> ProcurementRecommendation:
        comparison = tuple(
            ComparisonRow(
                offer_id=c["offer_id"],
                supplier_id=c["supplier_id"],
                score=parse_money_amount(c["score"]),
                rank=int(c["rank"]),
                currency=c["currency"],
                total_or_unit=c.get("total_or_unit"),
                mandatory_spec_failed=bool(c.get("mandatory_spec_failed")),
                flags=tuple(c.get("flags") or ()),
                breakdown=c.get("breakdown") or {},
            )
            for c in _json_loads(row["comparison_json"], [])
        )
        risks = tuple(
            RiskFinding(
                category=r["category"],
                level=r["level"],
                code=r["code"],
                message=r["message"],
                offer_id=r.get("offer_id"),
                supplier_id=r.get("supplier_id"),
            )
            for r in _json_loads(row["risks_json"], [])
        )
        return ProcurementRecommendation(
            recommendation_id=row["recommendation_id"],
            request_id=row["request_id"],
            scope=MemoryScope(scope_type=row["scope_type"], scope_id=row["scope_id"]),
            recommended_supplier_id=row["recommended_supplier_id"],
            recommended_offer_id=row["recommended_offer_id"],
            alternatives=tuple(_json_loads(row["alternatives_json"], [])),
            reasoning_summary=row["reasoning_summary"],
            comparison=comparison,
            risks=risks,
            assumptions=tuple(_json_loads(row["assumptions_json"], [])),
            missing_information=tuple(_json_loads(row["missing_information_json"], [])),
            confidence=float(row["confidence"]),
            citations=tuple(_json_loads(row["citations_json"], [])),
            requires_approval=bool(row["requires_approval"]),
            status=row["status"],
            single_source_procurement=bool(row["single_source_procurement"]),
            currency_conversion_required=bool(row["currency_conversion_required"]),
            metadata_safe=_json_loads(row["metadata_json"], {}),
            created_at=_dt_from_db(row["created_at"]),
        )

    def _comparison_to_dict(self, row: ComparisonRow) -> dict:
        return {
            "offer_id": row.offer_id,
            "supplier_id": row.supplier_id,
            "score": format(row.score, "f"),
            "rank": row.rank,
            "currency": row.currency,
            "total_or_unit": row.total_or_unit,
            "mandatory_spec_failed": row.mandatory_spec_failed,
            "flags": list(row.flags),
            "breakdown": dict(row.breakdown),
        }

    def _risk_to_dict(self, row: RiskFinding) -> dict:
        return {
            "category": row.category,
            "level": row.level,
            "code": row.code,
            "message": row.message,
            "offer_id": row.offer_id,
            "supplier_id": row.supplier_id,
        }

    def _requirement_to_dict(self, requirement: ProcurementRequirement) -> dict:
        return {
            "category": requirement.category,
            "normalized_item": requirement.normalized_item,
            "quantity": _dec_to_db(requirement.quantity),
            "unit": requirement.unit,
            "mandatory_specs": dict(requirement.mandatory_specs),
            "preferred_specs": dict(requirement.preferred_specs),
            "budget_constraint": requirement.budget_constraint.as_dict()
            if requirement.budget_constraint
            else None,
            "currency": requirement.currency,
            "delivery_deadline": _dt_to_db(requirement.delivery_deadline),
            "delivery_location": requirement.delivery_location,
            "supplier_constraints": dict(requirement.supplier_constraints),
            "compliance_constraints": dict(requirement.compliance_constraints),
            "notes": requirement.notes,
            "incomplete": requirement.incomplete,
            "missing_fields": list(requirement.missing_fields),
        }

    def _dict_to_requirement(self, data: dict) -> ProcurementRequirement:
        budget = None
        if data.get("budget_constraint"):
            b = data["budget_constraint"]
            budget = Money(amount=parse_money_amount(b["amount"]), currency=b["currency"])
        return ProcurementRequirement(
            category=data.get("category") or "general",
            normalized_item=data.get("normalized_item") or "",
            quantity=_dec_from_db(data.get("quantity")),
            unit=data.get("unit"),
            mandatory_specs=data.get("mandatory_specs") or {},
            preferred_specs=data.get("preferred_specs") or {},
            budget_constraint=budget,
            currency=data.get("currency"),
            delivery_deadline=_dt_from_db(data.get("delivery_deadline")),
            delivery_location=data.get("delivery_location"),
            supplier_constraints=data.get("supplier_constraints") or {},
            compliance_constraints=data.get("compliance_constraints") or {},
            notes=data.get("notes"),
            incomplete=bool(data.get("incomplete")),
            missing_fields=tuple(data.get("missing_fields") or ()),
        )


class SqliteSupplierRepository:
    """Thin adapter — supplier API over SqliteProcurementStore."""

    def __init__(self, store: SqliteProcurementStore):
        self.store = store

    def upsert(self, supplier: Supplier) -> Supplier:
        return self.store._upsert_supplier(supplier)

    def get(self, supplier_id: str, *, requesting_scope) -> Supplier | None:
        return self.store._get_supplier(supplier_id, requesting_scope=requesting_scope)

    def list_for_scope(self, scope) -> tuple[Supplier, ...]:
        return self.store.list_for_scope(scope)

    def find(self, **kwargs) -> tuple[Supplier, ...]:
        return self.store.find(**kwargs)


class SqliteOfferRepository:
    """Thin adapter — offer API over SqliteProcurementStore."""

    def __init__(self, store: SqliteProcurementStore):
        self.store = store

    def upsert(self, offer: SupplierOffer) -> SupplierOffer:
        return self.store._upsert_offer(offer)

    def get(self, offer_id: str, *, requesting_scope) -> SupplierOffer | None:
        return self.store._get_offer(offer_id, requesting_scope=requesting_scope)

    def list_for_request(self, request_id: str, *, scope) -> tuple[SupplierOffer, ...]:
        return self.store.list_for_request(request_id, scope=scope)
