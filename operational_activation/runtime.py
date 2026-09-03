"""Runtime for operational activation status API."""

from __future__ import annotations

from dataclasses import dataclass

from operational_activation.hitl_write import HitlWriteGovernor
from operational_activation.registry import block_status_report


@dataclass
class OperationalActivationRuntime:
    write_governor: HitlWriteGovernor

    def status(self) -> dict:
        report = block_status_report()
        report["26_hitl_write"]["real_write_count"] = self.write_governor.real_external_writes
        return report


def build_operational_activation_runtime() -> OperationalActivationRuntime:
    return OperationalActivationRuntime(write_governor=HitlWriteGovernor())
