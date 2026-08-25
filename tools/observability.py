"""In-process ToolGateway observability hooks (no high-cardinality labels)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class ToolMetrics:
    tool_calls_total: int = 0
    tool_success_total: int = 0
    tool_failure_total: int = 0
    tool_denied_total: int = 0
    tool_timeout_total: int = 0
    tool_uncertain_total: int = 0
    # Labels: (tool_id, operation, trust_level) — never raw resource.
    by_tool: dict[tuple[str, str, str], dict[str, int | float]] = field(
        default_factory=lambda: defaultdict(
            lambda: {
                "calls": 0,
                "success": 0,
                "failure": 0,
                "denied": 0,
                "timeout": 0,
                "uncertain": 0,
                "latency_ms_sum": 0.0,
            }
        )
    )
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(
        self,
        *,
        tool_id: str,
        operation: str,
        trust_level: str,
        outcome: str,
        latency_ms: int = 0,
    ) -> None:
        key = (str(tool_id), str(operation), str(trust_level))
        with self._lock:
            self.tool_calls_total += 1
            bucket = self.by_tool[key]
            bucket["calls"] = int(bucket["calls"]) + 1
            bucket["latency_ms_sum"] = float(bucket["latency_ms_sum"]) + float(latency_ms)
            if outcome == "success":
                self.tool_success_total += 1
                bucket["success"] = int(bucket["success"]) + 1
            elif outcome == "denied":
                self.tool_denied_total += 1
                bucket["denied"] = int(bucket["denied"]) + 1
            elif outcome == "timeout":
                self.tool_timeout_total += 1
                bucket["timeout"] = int(bucket["timeout"]) + 1
            elif outcome == "uncertain":
                self.tool_uncertain_total += 1
                bucket["uncertain"] = int(bucket["uncertain"]) + 1
            else:
                self.tool_failure_total += 1
                bucket["failure"] = int(bucket["failure"]) + 1

    def snapshot(self) -> dict:
        with self._lock:
            by_tool = {
                f"{k[0]}|{k[1]}|{k[2]}": dict(v) for k, v in self.by_tool.items()
            }
            return {
                "tool_calls_total": self.tool_calls_total,
                "tool_success_total": self.tool_success_total,
                "tool_failure_total": self.tool_failure_total,
                "tool_denied_total": self.tool_denied_total,
                "tool_timeout_total": self.tool_timeout_total,
                "tool_uncertain_total": self.tool_uncertain_total,
                "by_tool": by_tool,
            }
