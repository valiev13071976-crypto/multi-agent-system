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

    def get(self, approval_id: str) -> ApprovalRecord | None:
        raise NotImplementedError

    def list_for_action(self, action_id: str) -> tuple[ApprovalRecord, ...]:
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


class InMemoryIdempotencyStore(IdempotencyStore):
    def __init__(self):
        self._items: dict[str, IdempotencyRecord] = {}

    def put(self, record: IdempotencyRecord) -> None:
        self._items[record.key] = record

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._items.get(key)
