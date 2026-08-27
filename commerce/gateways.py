"""Source-of-Truth gateways — Panda orchestrates; external systems own legal truth."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Mapping, Protocol

from commerce.contracts import ExternalConfirmation, InventoryPosition
from commerce.errors import ExternalUnconfirmedError, InsufficientStockError, OversellError
from commerce.states import (
    EDO_ACCEPTED,
    EDO_DRAFT,
    EDO_PREPARED,
    EDO_SENT,
    EDO_SIGNED,
    FISCAL_FISCALIZED,
    FISCAL_OFD_CONFIRMED,
    FISCAL_PENDING,
    FISCAL_SUBMITTED,
    MARKING_AVAILABLE,
    MARKING_TRANSFERRED,
    MARKING_WITHDRAWN,
    assert_transition,
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _conf(system: str, status: str, **extra) -> ExternalConfirmation:
    return ExternalConfirmation(
        system=system,
        external_id=f"{system}-{uuid.uuid4().hex[:12]}",
        status=status,
        timestamp=_utc(),
        provenance=extra,
    )


class InventoryGateway(Protocol):
    def snapshot(self, *, tenant_id: str, product_ref: str, warehouse: str) -> InventoryPosition: ...
    def reserve(self, *, tenant_id: str, product_ref: str, warehouse: str, qty: float, idempotency_key: str) -> ExternalConfirmation: ...
    def release(self, *, tenant_id: str, reservation_external_id: str, idempotency_key: str) -> ExternalConfirmation: ...
    def receive(self, *, tenant_id: str, product_ref: str, warehouse: str, qty: float, idempotency_key: str) -> ExternalConfirmation: ...
    def adjust(self, *, tenant_id: str, product_ref: str, warehouse: str, delta: float, idempotency_key: str) -> ExternalConfirmation: ...


class AccountingGateway(Protocol):
    def create_receipt(self, *, tenant_id: str, payload: Mapping, idempotency_key: str) -> ExternalConfirmation: ...
    def get_document_status(self, *, tenant_id: str, external_id: str) -> ExternalConfirmation: ...


class EdoGateway(Protocol):
    def prepare_document(self, *, tenant_id: str, payload: Mapping, idempotency_key: str) -> ExternalConfirmation: ...
    def send_document(self, *, tenant_id: str, document_external_id: str, idempotency_key: str) -> ExternalConfirmation: ...
    def get_document_status(self, *, tenant_id: str, document_external_id: str) -> ExternalConfirmation: ...
    def attach_marking_codes(self, *, tenant_id: str, document_external_id: str, codes: tuple[str, ...], idempotency_key: str) -> ExternalConfirmation: ...


class MarkingGateway(Protocol):
    def read_status(self, *, tenant_id: str, code_ref: str) -> ExternalConfirmation: ...
    def transfer(self, *, tenant_id: str, code_ref: str, to_owner: str, idempotency_key: str) -> ExternalConfirmation: ...
    def withdraw(self, *, tenant_id: str, code_ref: str, idempotency_key: str) -> ExternalConfirmation: ...
    def reintroduce(self, *, tenant_id: str, code_ref: str, idempotency_key: str) -> ExternalConfirmation: ...


class FiscalGateway(Protocol):
    def create_receipt(self, *, tenant_id: str, payload: Mapping, idempotency_key: str) -> ExternalConfirmation: ...
    def get_receipt_status(self, *, tenant_id: str, receipt_external_id: str) -> ExternalConfirmation: ...
    def refund_receipt(self, *, tenant_id: str, receipt_external_id: str, idempotency_key: str) -> ExternalConfirmation: ...


class CommerceFrontOfficeGateway(Protocol):
    def pull_order(self, *, tenant_id: str, external_order_id: str) -> Mapping: ...
    def push_stock(self, *, tenant_id: str, product_ref: str, available: float, warehouse: str) -> ExternalConfirmation: ...


class FakeInventoryGateway:
    """Test/dev SoT — confirms externally; Panda must not treat local alone as truth."""

    system = "moysklad"

    def __init__(self):
        self._stock: dict[tuple[str, str, str], InventoryPosition] = {}
        self._reservations: dict[str, dict] = {}
        self._ops: set[str] = set()

    def seed(self, pos: InventoryPosition, *, tenant_id: str) -> None:
        key = (tenant_id, pos.product_ref, pos.warehouse)
        self._stock[key] = pos

    def snapshot(self, *, tenant_id: str, product_ref: str, warehouse: str) -> InventoryPosition:
        pos = self._stock.get((tenant_id, product_ref, warehouse))
        if pos is None:
            return InventoryPosition(
                product_ref=product_ref,
                warehouse=warehouse,
                available=0.0,
                fetched_at=_utc(),
                source=self.system,
            )
        return InventoryPosition(
            product_ref=pos.product_ref,
            warehouse=pos.warehouse,
            available=pos.available,
            reserved=pos.reserved,
            in_transit=pos.in_transit,
            expected=pos.expected,
            blocked=pos.blocked,
            lot=pos.lot,
            serial=pos.serial,
            marking=pos.marking,
            cost_ref=pos.cost_ref,
            # Preserve fixture timestamps so stale_after can block critical ops
            fetched_at=pos.fetched_at or _utc(),
            external_updated_at=pos.external_updated_at or pos.fetched_at or _utc(),
            stale_after_seconds=pos.stale_after_seconds,
            source=self.system,
            external_id=pos.external_id or f"inv-{product_ref}",
        )

    def reserve(self, *, tenant_id: str, product_ref: str, warehouse: str, qty: float, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            existing = self._reservations.get(idempotency_key)
            return ExternalConfirmation(
                system=self.system,
                external_id=existing["external_id"],
                status="reserved",
                timestamp=_utc(),
                provenance={"idempotent": True},
            )
        pos = self.snapshot(tenant_id=tenant_id, product_ref=product_ref, warehouse=warehouse)
        if pos.is_stale():
            from commerce.errors import StaleStateError

            raise StaleStateError("stale_state")
        if pos.available < qty:
            raise InsufficientStockError("insufficient_stock")
        if pos.available - qty < 0:
            raise OversellError("oversell_prevented")
        updated = InventoryPosition(
            product_ref=product_ref,
            warehouse=warehouse,
            available=pos.available - qty,
            reserved=pos.reserved + qty,
            fetched_at=_utc(),
            external_updated_at=_utc(),
            stale_after_seconds=pos.stale_after_seconds,
            source=self.system,
            external_id=pos.external_id,
        )
        self._stock[(tenant_id, product_ref, warehouse)] = updated
        conf = _conf(self.system, "reserved", qty=qty, product_ref=product_ref)
        self._reservations[idempotency_key] = {"external_id": conf.external_id, "qty": qty}
        self._ops.add(idempotency_key)
        return conf

    def release(self, *, tenant_id: str, reservation_external_id: str, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return _conf(self.system, "released", idempotent=True)
        self._ops.add(idempotency_key)
        return _conf(self.system, "released", reservation=reservation_external_id)

    def receive(self, *, tenant_id: str, product_ref: str, warehouse: str, qty: float, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return _conf(self.system, "received", idempotent=True)
        pos = self.snapshot(tenant_id=tenant_id, product_ref=product_ref, warehouse=warehouse)
        self._stock[(tenant_id, product_ref, warehouse)] = InventoryPosition(
            product_ref=product_ref,
            warehouse=warehouse,
            available=pos.available + qty,
            reserved=pos.reserved,
            fetched_at=_utc(),
            external_updated_at=_utc(),
            source=self.system,
        )
        self._ops.add(idempotency_key)
        return _conf(self.system, "received", qty=qty)

    def adjust(self, *, tenant_id: str, product_ref: str, warehouse: str, delta: float, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return _conf(self.system, "adjusted", idempotent=True)
        pos = self.snapshot(tenant_id=tenant_id, product_ref=product_ref, warehouse=warehouse)
        self._stock[(tenant_id, product_ref, warehouse)] = InventoryPosition(
            product_ref=product_ref,
            warehouse=warehouse,
            available=pos.available + delta,
            reserved=pos.reserved,
            fetched_at=_utc(),
            external_updated_at=_utc(),
            source=self.system,
        )
        self._ops.add(idempotency_key)
        return _conf(self.system, "adjusted", delta=delta)


class FakeAccountingGateway:
    system = "1c"

    def __init__(self):
        self._docs: dict[str, ExternalConfirmation] = {}
        self._ops: set[str] = set()

    def create_receipt(self, *, tenant_id: str, payload: Mapping, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return self._docs[idempotency_key]
        conf = _conf(self.system, "created", kind="receipt")
        self._docs[idempotency_key] = conf
        self._docs[conf.external_id] = conf
        self._ops.add(idempotency_key)
        return conf

    def get_document_status(self, *, tenant_id: str, external_id: str) -> ExternalConfirmation:
        conf = self._docs.get(external_id)
        if conf is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        return conf


class FakeEdoGateway:
    system = "edo"

    def __init__(self):
        self._docs: dict[str, dict] = {}
        self._ops: set[str] = set()

    def prepare_document(self, *, tenant_id: str, payload: Mapping, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            d = self._docs[idempotency_key]
            return ExternalConfirmation(
                system=self.system, external_id=d["id"], status=d["status"], timestamp=_utc(),
                provenance={"idempotent": True},
            )
        conf = _conf(self.system, EDO_PREPARED)
        self._docs[idempotency_key] = {"id": conf.external_id, "status": EDO_PREPARED, "codes": ()}
        self._docs[conf.external_id] = self._docs[idempotency_key]
        self._ops.add(idempotency_key)
        return conf

    def send_document(self, *, tenant_id: str, document_external_id: str, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            d = self._docs[document_external_id]
            return ExternalConfirmation(
                system=self.system, external_id=d["id"], status=d["status"], timestamp=_utc(),
                provenance={"idempotent": True},
            )
        d = self._docs.get(document_external_id)
        if d is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        # Foundation: prepare → sign → send as one confirmed external progression
        if d["status"] == EDO_PREPARED:
            assert_transition("edo", EDO_PREPARED, EDO_SIGNED)
            d["status"] = EDO_SIGNED
        if d["status"] == EDO_SIGNED:
            assert_transition("edo", EDO_SIGNED, EDO_SENT)
            d["status"] = EDO_SENT
        else:
            assert_transition("edo", d["status"], EDO_SENT)
            d["status"] = EDO_SENT
        self._ops.add(idempotency_key)
        return ExternalConfirmation(
            system=self.system, external_id=document_external_id, status=EDO_SENT, timestamp=_utc()
        )

    def get_document_status(self, *, tenant_id: str, document_external_id: str) -> ExternalConfirmation:
        d = self._docs.get(document_external_id)
        if d is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        return ExternalConfirmation(
            system=self.system, external_id=document_external_id, status=d["status"], timestamp=_utc()
        )

    def attach_marking_codes(self, *, tenant_id: str, document_external_id: str, codes: tuple[str, ...], idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return self.get_document_status(tenant_id=tenant_id, document_external_id=document_external_id)
        d = self._docs.get(document_external_id)
        if d is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        d["codes"] = tuple(codes)
        self._ops.add(idempotency_key)
        return ExternalConfirmation(
            system=self.system,
            external_id=document_external_id,
            status=d["status"],
            timestamp=_utc(),
            provenance={"codes_attached": len(codes)},
        )


class FakeMarkingGateway:
    system = "marking"

    def __init__(self):
        self._codes: dict[tuple[str, str], str] = {}
        self._ops: set[str] = set()

    def seed(self, *, tenant_id: str, code_ref: str, status: str = MARKING_AVAILABLE) -> None:
        self._codes[(tenant_id, code_ref)] = status

    def read_status(self, *, tenant_id: str, code_ref: str) -> ExternalConfirmation:
        status = self._codes.get((tenant_id, code_ref), MARKING_AVAILABLE)
        return ExternalConfirmation(
            system=self.system, external_id=code_ref, status=status, timestamp=_utc()
        )

    def transfer(self, *, tenant_id: str, code_ref: str, to_owner: str, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return self.read_status(tenant_id=tenant_id, code_ref=code_ref)
        cur = self._codes.get((tenant_id, code_ref), MARKING_AVAILABLE)
        assert_transition("marking", cur, MARKING_TRANSFERRED)
        self._codes[(tenant_id, code_ref)] = MARKING_TRANSFERRED
        self._ops.add(idempotency_key)
        return ExternalConfirmation(
            system=self.system,
            external_id=code_ref,
            status=MARKING_TRANSFERRED,
            timestamp=_utc(),
            provenance={"to_owner": to_owner},
        )

    def withdraw(self, *, tenant_id: str, code_ref: str, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return self.read_status(tenant_id=tenant_id, code_ref=code_ref)
        cur = self._codes.get((tenant_id, code_ref), MARKING_AVAILABLE)
        assert_transition("marking", cur, MARKING_WITHDRAWN)
        self._codes[(tenant_id, code_ref)] = MARKING_WITHDRAWN
        self._ops.add(idempotency_key)
        return ExternalConfirmation(
            system=self.system, external_id=code_ref, status=MARKING_WITHDRAWN, timestamp=_utc()
        )

    def reintroduce(self, *, tenant_id: str, code_ref: str, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return self.read_status(tenant_id=tenant_id, code_ref=code_ref)
        from commerce.states import MARKING_REINTRODUCED

        cur = self._codes.get((tenant_id, code_ref), MARKING_WITHDRAWN)
        assert_transition("marking", cur, MARKING_REINTRODUCED)
        self._codes[(tenant_id, code_ref)] = MARKING_REINTRODUCED
        self._ops.add(idempotency_key)
        return ExternalConfirmation(
            system=self.system, external_id=code_ref, status=MARKING_REINTRODUCED, timestamp=_utc()
        )


class FakeFiscalGateway:
    system = "kkt"

    def __init__(self):
        self._receipts: dict[str, dict] = {}
        self._ops: set[str] = set()

    def create_receipt(self, *, tenant_id: str, payload: Mapping, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            r = self._receipts[idempotency_key]
            return ExternalConfirmation(
                system=self.system, external_id=r["id"], status=r["status"], timestamp=_utc(),
                provenance={"idempotent": True},
            )
        conf = _conf(self.system, FISCAL_SUBMITTED)
        self._receipts[idempotency_key] = {"id": conf.external_id, "status": FISCAL_OFD_CONFIRMED}
        self._receipts[conf.external_id] = self._receipts[idempotency_key]
        self._ops.add(idempotency_key)
        return ExternalConfirmation(
            system=self.system,
            external_id=conf.external_id,
            status=FISCAL_OFD_CONFIRMED,
            timestamp=_utc(),
        )

    def get_receipt_status(self, *, tenant_id: str, receipt_external_id: str) -> ExternalConfirmation:
        r = self._receipts.get(receipt_external_id)
        if r is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        return ExternalConfirmation(
            system=self.system, external_id=receipt_external_id, status=r["status"], timestamp=_utc()
        )

    def refund_receipt(self, *, tenant_id: str, receipt_external_id: str, idempotency_key: str) -> ExternalConfirmation:
        if idempotency_key in self._ops:
            return self.get_receipt_status(tenant_id=tenant_id, receipt_external_id=receipt_external_id)
        from commerce.states import FISCAL_REFUNDED

        r = self._receipts.get(receipt_external_id)
        if r is None:
            raise ExternalUnconfirmedError("external_unconfirmed")
        r["status"] = FISCAL_REFUNDED
        self._ops.add(idempotency_key)
        return ExternalConfirmation(
            system=self.system, external_id=receipt_external_id, status=FISCAL_REFUNDED, timestamp=_utc()
        )


class FakeFrontOfficeGateway:
    system = "bitrix"

    def __init__(self):
        self._orders: dict[tuple[str, str], dict] = {}

    def seed_order(self, *, tenant_id: str, external_order_id: str, payload: dict) -> None:
        self._orders[(tenant_id, external_order_id)] = dict(payload)

    def pull_order(self, *, tenant_id: str, external_order_id: str) -> Mapping:
        return dict(self._orders.get((tenant_id, external_order_id)) or {})

    def push_stock(self, *, tenant_id: str, product_ref: str, available: float, warehouse: str) -> ExternalConfirmation:
        return _conf(self.system, "stock_pushed", product_ref=product_ref, available=available)
