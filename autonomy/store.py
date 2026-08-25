from autonomy.models import ApprovalRecord, IdempotencyRecord
from autonomy.tokens import CapabilityToken


class CapabilityTokenStore:
    def put(self, token: CapabilityToken) -> None:
        raise NotImplementedError

    def get(self, token_id: str) -> CapabilityToken | None:
        raise NotImplementedError

    def revoke(self, token_id: str) -> None:
        raise NotImplementedError

    def is_revoked(self, token_id: str) -> bool:
        raise NotImplementedError


class ApprovalStore:
    def put(self, record: ApprovalRecord) -> None:
        raise NotImplementedError

    def create(self, record: ApprovalRecord) -> None:
        self.put(record)

    def save(self, record: ApprovalRecord) -> None:
        self.put(record)

    def get(self, approval_id: str) -> ApprovalRecord | None:
        raise NotImplementedError

    def list_for_action(self, action_id: str) -> tuple[ApprovalRecord, ...]:
        raise NotImplementedError

    def find_pending_by_action(self, action_id: str) -> ApprovalRecord | None:
        raise NotImplementedError

    def list_pending(self) -> tuple[ApprovalRecord, ...]:
        raise NotImplementedError

    def list_by_workflow(self, workflow_id: str) -> tuple[ApprovalRecord, ...]:
        raise NotImplementedError

    def list_by_status(self, status: str) -> tuple[ApprovalRecord, ...]:
        raise NotImplementedError

    def list_all(self) -> tuple[ApprovalRecord, ...]:
        raise NotImplementedError


class IdempotencyStore:
    def put(self, record: IdempotencyRecord) -> None:
        raise NotImplementedError

    def get(self, key: str) -> IdempotencyRecord | None:
        raise NotImplementedError


class InMemoryCapabilityTokenStore(CapabilityTokenStore):
    def __init__(self):
        self._items: dict[str, CapabilityToken] = {}
        self._revoked: set[str] = set()

    def put(self, token: CapabilityToken) -> None:
        self._items[token.token_id] = token

    def get(self, token_id: str) -> CapabilityToken | None:
        return self._items.get(token_id)

    def revoke(self, token_id: str) -> None:
        self._revoked.add(token_id)

    def is_revoked(self, token_id: str) -> bool:
        return token_id in self._revoked


class InMemoryApprovalStore(ApprovalStore):
    def __init__(self):
        self._items: dict[str, ApprovalRecord] = {}

    def put(self, record: ApprovalRecord) -> None:
        self._items[record.approval_id] = record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._items.get(approval_id)

    def list_for_action(self, action_id: str) -> tuple[ApprovalRecord, ...]:
        return tuple(
            item for item in self._items.values() if item.action_id == action_id
        )

    def find_pending_by_action(self, action_id: str) -> ApprovalRecord | None:
        from autonomy.models import APPROVAL_PENDING

        for item in self._items.values():
            if item.action_id == action_id and item.status == APPROVAL_PENDING:
                return item
        return None

    def list_pending(self) -> tuple[ApprovalRecord, ...]:
        from autonomy.models import APPROVAL_PENDING

        return tuple(
            item for item in self._items.values() if item.status == APPROVAL_PENDING
        )

    def list_by_workflow(self, workflow_id: str) -> tuple[ApprovalRecord, ...]:
        return tuple(
            item for item in self._items.values() if item.workflow_id == workflow_id
        )

    def list_by_status(self, status: str) -> tuple[ApprovalRecord, ...]:
        return tuple(item for item in self._items.values() if item.status == status)

    def list_all(self) -> tuple[ApprovalRecord, ...]:
        return tuple(self._items.values())


class InMemoryIdempotencyStore(IdempotencyStore):
    def __init__(self):
        self._items: dict[str, IdempotencyRecord] = {}

    def put(self, record: IdempotencyRecord) -> None:
        self._items[record.key] = record

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._items.get(key)
