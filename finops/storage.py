from datetime import datetime, timezone

from finops.models import UsageRecord


class UsageStore:
    def add(self, record: UsageRecord) -> None:
        raise NotImplementedError

    def records(self) -> tuple[UsageRecord, ...]:
        raise NotImplementedError

    def records_between(self, start: datetime, end: datetime) -> tuple[UsageRecord, ...]:
        raise NotImplementedError

    def records_for_task(self, task_id: str) -> tuple[UsageRecord, ...]:
        raise NotImplementedError


class InMemoryUsageStore(UsageStore):
    def __init__(self):
        self._records: list[UsageRecord] = []

    def add(self, record: UsageRecord) -> None:
        self._records.append(record)

    def records(self) -> tuple[UsageRecord, ...]:
        return tuple(self._records)

    def records_between(self, start: datetime, end: datetime) -> tuple[UsageRecord, ...]:
        selected = []
        for record in self._records:
            stamp = record.timestamp
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if start <= stamp < end:
                selected.append(record)
        return tuple(selected)

    def records_for_task(self, task_id: str) -> tuple[UsageRecord, ...]:
        return tuple(
            record for record in self._records if record.task_id == task_id
        )
