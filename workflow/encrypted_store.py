from security.encryption import (
    ENCRYPTION_REQUIRED,
    EncryptedStore,
    EncryptionService,
    EncryptionUnavailableError,
)
from workflow.models import Checkpoint
from workflow.store import InMemoryWorkflowStateStore, WorkflowStateStore


class EncryptedWorkflowStateStore(WorkflowStateStore):
    """
    Persistence wrapper. In-memory public/internal checkpoints stay plaintext.
    Sensitive/secret checkpoint payloads require EncryptionService and
    never fall back to plaintext.
    """

    def __init__(
        self,
        inner: WorkflowStateStore | None = None,
        encryption: EncryptionService | None = None,
        encrypted_store: EncryptedStore | None = None,
    ):
        self._inner = inner or InMemoryWorkflowStateStore()
        self._encryption = encryption
        self._encrypted = encrypted_store

    def create(self, state):
        self._inner.create(state)

    def get(self, workflow_id: str):
        return self._inner.get(workflow_id)

    def save(self, state):
        self._inner.save(state)

    def checkpoint(self, checkpoint: Checkpoint) -> None:
        if checkpoint.sensitivity in ENCRYPTION_REQUIRED:
            if self._encryption is None:
                raise EncryptionUnavailableError(
                    "Sensitive workflow checkpoint requires EncryptionService."
                )
            serialized = str(dict(checkpoint.payload))
            encrypted = self._encryption.encrypt(serialized)
            if self._encrypted is not None:
                self._encrypted.put(
                    f"checkpoint:{checkpoint.workflow_id}",
                    encrypted.serialize(),
                    checkpoint.sensitivity,
                )
            sealed = Checkpoint(
                workflow_id=checkpoint.workflow_id,
                workflow_version=checkpoint.workflow_version,
                status=checkpoint.status,
                current_step=checkpoint.current_step,
                completed_steps=checkpoint.completed_steps,
                timestamp=checkpoint.timestamp,
                payload={
                    "encrypted": True,
                    "kid": encrypted.key_id,
                    "alg": encrypted.algorithm,
                },
                sensitivity=checkpoint.sensitivity,
            )
            self._inner.checkpoint(sealed)
            return
        self._inner.checkpoint(checkpoint)

    def get_checkpoint(self, workflow_id: str) -> Checkpoint | None:
        return self._inner.get_checkpoint(workflow_id)

    def list_by_status(self, status: str):
        if hasattr(self._inner, "list_by_status"):
            return self._inner.list_by_status(status)
        return ()

    def list_all(self):
        if hasattr(self._inner, "list_all"):
            return self._inner.list_all()
        return ()

    def find_by_execution_key(self, execution_key: str, *, tenant_id: str | None = None):
        if hasattr(self._inner, "find_by_execution_key"):
            return self._inner.find_by_execution_key(
                execution_key, tenant_id=tenant_id
            )
        return None
