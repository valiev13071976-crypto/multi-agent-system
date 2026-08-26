"""In-memory supplier and offer repositories."""

from __future__ import annotations

from procurement.errors import PROCUREMENT_SCOPE_DENIED, ProcurementError
from procurement.models import ProcurementRecommendation, ProcurementRequest, Supplier, SupplierOffer


class InMemorySupplierRepository:
    def __init__(self):
        self._rows: dict[str, Supplier] = {}

    def upsert(self, supplier: Supplier) -> Supplier:
        self._rows[supplier.supplier_id] = supplier
        return supplier

    def get(self, supplier_id: str, *, requesting_scope) -> Supplier | None:
        row = self._rows.get(supplier_id)
        if row is None:
            return None
        if row.scope.key() != requesting_scope.key():
            raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
        return row

    def list_for_scope(self, scope) -> tuple[Supplier, ...]:
        return tuple(s for s in self._rows.values() if s.scope.key() == scope.key())

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
                s for s in rows if cats.intersection({c.lower() for c in s.categories}) or not s.categories
            )
        if statuses:
            wanted = set(statuses)
            rows = tuple(s for s in rows if s.status in wanted)
        if exclude_restricted:
            rows = tuple(s for s in rows if s.status != "restricted")
        return rows


class InMemoryOfferRepository:
    def __init__(self):
        self._rows: dict[str, SupplierOffer] = {}

    def upsert(self, offer: SupplierOffer) -> SupplierOffer:
        self._rows[offer.offer_id] = offer
        return offer

    def get(self, offer_id: str, *, requesting_scope) -> SupplierOffer | None:
        row = self._rows.get(offer_id)
        if row is None:
            return None
        if row.scope.key() != requesting_scope.key():
            raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
        return row

    def list_for_request(self, request_id: str, *, scope) -> tuple[SupplierOffer, ...]:
        return tuple(
            o
            for o in self._rows.values()
            if o.request_id == request_id and o.scope.key() == scope.key()
        )


class InMemoryRequestStore:
    def __init__(self):
        self._requests: dict[str, ProcurementRequest] = {}
        self._requirements: dict[str, object] = {}
        self._recommendations: dict[str, ProcurementRecommendation] = {}

    def save_request(self, request: ProcurementRequest) -> ProcurementRequest:
        self._requests[request.request_id] = request
        return request

    def get_request(self, request_id: str, *, requesting_scope) -> ProcurementRequest | None:
        row = self._requests.get(request_id)
        if row is None:
            return None
        if row.scope.key() != requesting_scope.key():
            raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
        return row

    def save_requirement(self, request_id: str, requirement) -> None:
        self._requirements[request_id] = requirement

    def get_requirement(self, request_id: str):
        return self._requirements.get(request_id)

    def save_recommendation(self, rec: ProcurementRecommendation) -> ProcurementRecommendation:
        self._recommendations[rec.recommendation_id] = rec
        self._recommendations[f"req:{rec.request_id}"] = rec
        return rec

    def get_recommendation_for_request(
        self, request_id: str, *, requesting_scope
    ) -> ProcurementRecommendation | None:
        row = self._recommendations.get(f"req:{request_id}")
        if row is None:
            return None
        if row.scope.key() != requesting_scope.key():
            raise ProcurementError(PROCUREMENT_SCOPE_DENIED)
        return row
