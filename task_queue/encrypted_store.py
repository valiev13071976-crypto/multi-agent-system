from security.encryption import (
    ENCRYPTION_REQUIRED,
    EncryptedStore,
    EncryptionService,
    EncryptionUnavailableError,
)
from task_queue.store import InMemoryTaskQueueStore, TaskQueueStore


class EncryptedTaskQueueStore(TaskQueueStore):
    """
    Persistence wrapper. Internal in-memory metadata stays plaintext.
    Sensitive/secret payloads require EncryptionService; no plaintext fallback.
    """

    def __init__(
        self,
        inner: TaskQueueStore | None = None,
        encryption: EncryptionService | None = None,
        encrypted_store: EncryptedStore | None = None,
    ):
        self._inner = inner or InMemoryTaskQueueStore()
        self._encryption = encryption
        self._encrypted = encrypted_store

    def enqueue(self, task):
        self._inner.enqueue(task)

    def get(self, queue_task_id: str):
        return self._inner.get(queue_task_id)

    def get_for_tenant(self, queue_task_id: str, tenant_id: str):
        if hasattr(self._inner, "get_for_tenant"):
            return self._inner.get_for_tenant(queue_task_id, tenant_id)
        tid = str(tenant_id or "").strip()
        if not tid:
            return None
        task = self.get(queue_task_id)
        if task is None:
            return None
        if str(getattr(task, "tenant_id", "") or "").strip() != tid:
            return None
        return task

    def save(self, task):
        self._inner.save(task)

    def list_ready(self):
        return self._inner.list_ready()

    def get_dead_letters(self):
        return self._inner.get_dead_letters()

    def find_by_execution_key(self, execution_key: str):
        return self._inner.find_by_execution_key(execution_key)

    def put_sensitive_payload(self, key: str, value: str, sensitivity: str) -> None:
        if sensitivity in ENCRYPTION_REQUIRED:
            if self._encryption is None:
                raise EncryptionUnavailableError(
                    "Sensitive queue payload requires EncryptionService."
                )
            encrypted = self._encryption.encrypt(value)
            if self._encrypted is not None:
                self._encrypted.put(key, encrypted.serialize(), sensitivity)
            return
        if self._encrypted is not None:
            self._encrypted.put(key, value, sensitivity)
