class QueueError(Exception):
    pass


class QueueTaskNotFoundError(QueueError):
    def __init__(self, queue_task_id: str):
        self.queue_task_id = queue_task_id
        super().__init__(f"Queue task not found: {queue_task_id}")


class QueueTransitionError(QueueError):
    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Invalid queue transition: {current} -> {target}")


class QueueLeaseError(QueueError):
    pass


class QueueDuplicateExecutionError(QueueError):
    def __init__(self, execution_key: str):
        self.execution_key = execution_key
        super().__init__(f"Duplicate execution_key is terminal: {execution_key}")


class QueueTimeoutError(QueueError):
    def __init__(self):
        super().__init__("execution_timeout")


class QueueCancelledError(QueueError):
    pass


class QueueTenantOwnershipError(QueueError):
    """Fail-closed when tenant_id does not own the queue task."""

    def __init__(self, reason: str = "tenant_mismatch"):
        self.reason = str(reason)
        super().__init__(self.reason)
