from task_queue.models import QueueTask


class TaskQueueStore:
    def enqueue(self, task: QueueTask) -> None:
        raise NotImplementedError

    def get(self, queue_task_id: str) -> QueueTask | None:
        raise NotImplementedError

    def list_ready(self) -> tuple[QueueTask, ...]:
        raise NotImplementedError

    def save(self, task: QueueTask) -> None:
        raise NotImplementedError

    def get_dead_letters(self) -> tuple[QueueTask, ...]:
        raise NotImplementedError

    def find_by_execution_key(self, execution_key: str) -> tuple[QueueTask, ...]:
        raise NotImplementedError


class InMemoryTaskQueueStore(TaskQueueStore):
    def __init__(self):
        self._items: dict[str, QueueTask] = {}

    def enqueue(self, task: QueueTask) -> None:
        self._items[task.queue_task_id] = task

    def get(self, queue_task_id: str) -> QueueTask | None:
        return self._items.get(queue_task_id)

    def save(self, task: QueueTask) -> None:
        self._items[task.queue_task_id] = task

    def list_all(self) -> tuple[QueueTask, ...]:
        return tuple(self._items.values())

    def list_ready(self) -> tuple[QueueTask, ...]:
        return tuple(self._items.values())

    def get_dead_letters(self) -> tuple[QueueTask, ...]:
        from task_queue.models import STATUS_DEAD_LETTERED

        return tuple(
            item
            for item in self._items.values()
            if item.status == STATUS_DEAD_LETTERED
        )

    def find_by_execution_key(self, execution_key: str) -> tuple[QueueTask, ...]:
        return tuple(
            item
            for item in self._items.values()
            if item.execution_key == execution_key
        )
