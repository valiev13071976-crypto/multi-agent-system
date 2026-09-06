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


class QueueDLQIneligibleError(QueueError):
    """DLQ operation (replay/redrive) targets a task that is not eligible
    (Scale 3.27): wrong status, missing task, or malformed identifiers.
    """

    error_code = "DLQ_INELIGIBLE"

    def __init__(self, reason: str = "dlq_ineligible"):
        self.reason = str(reason)
        super().__init__(self.reason)


class QueueRedriveRejectedError(QueueError):
    """Redrive rejected by a safety policy (Scale 3.27), e.g. the bounded
    redrive-count guard that prevents an accidental infinite redrive loop.
    """

    error_code = "REDRIVE_REJECTED"

    def __init__(self, reason: str = "redrive_rejected"):
        self.reason = str(reason)
        super().__init__(self.reason)
