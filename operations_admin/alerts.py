"""Deterministic operational alert engine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from operations_admin.models import ALERT_CRITICAL, ALERT_INFO, ALERT_WARNING, AlertView


@dataclass
class _AlertState:
    alert_id: str
    severity: str
    source: str
    message: str
    status: str
    first_observed: str
    last_observed: str
    count: int = 1


class AlertEngine:
    def __init__(self):
        self._active: dict[str, _AlertState] = {}

    @staticmethod
    def _identity(source: str, message: str) -> str:
        raw = f"{source}:{message}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    def observe(self, *, source: str, message: str, severity: str, active: bool) -> AlertView | None:
        aid = self._identity(source, message)
        now = datetime.now(timezone.utc).isoformat()
        if active:
            existing = self._active.get(aid)
            if existing:
                existing.last_observed = now
                existing.count += 1
                existing.status = "active"
                st = existing
            else:
                st = _AlertState(
                    alert_id=aid,
                    severity=severity,
                    source=source,
                    message=message,
                    status="active",
                    first_observed=now,
                    last_observed=now,
                )
                self._active[aid] = st
            return AlertView(
                alert_id=st.alert_id,
                severity=st.severity,
                source=st.source,
                message=st.message,
                status=st.status,
                first_observed=st.first_observed,
                last_observed=st.last_observed,
                count=st.count,
            )
        if aid in self._active:
            st = self._active[aid]
            st.status = "resolved"
            st.last_observed = now
            return AlertView(
                alert_id=st.alert_id,
                severity=st.severity,
                source=st.source,
                message=st.message,
                status=st.status,
                first_observed=st.first_observed,
                last_observed=st.last_observed,
                count=st.count,
            )
        return None

    def list_active(self) -> list[AlertView]:
        return [
            AlertView(
                alert_id=st.alert_id,
                severity=st.severity,
                source=st.source,
                message=st.message,
                status=st.status,
                first_observed=st.first_observed,
                last_observed=st.last_observed,
                count=st.count,
            )
            for st in self._active.values()
            if st.status == "active"
        ]

    def evaluate_health(self, components: list[dict]) -> list[AlertView]:
        out: list[AlertView] = []
        for comp in components:
            name = str(comp.get("name") or "unknown")
            status = str(comp.get("status") or "unknown").lower()
            if status in {"unhealthy", "not_ready", "failed"}:
                a = self.observe(
                    source=name,
                    message=f"{name} unhealthy",
                    severity=ALERT_CRITICAL,
                    active=True,
                )
                if a:
                    out.append(a)
            elif status in {"degraded", "stale"}:
                a = self.observe(
                    source=name,
                    message=f"{name} degraded",
                    severity=ALERT_WARNING,
                    active=True,
                )
                if a:
                    out.append(a)
            else:
                self.observe(source=name, message=f"{name} unhealthy", severity=ALERT_WARNING, active=False)
                self.observe(source=name, message=f"{name} degraded", severity=ALERT_WARNING, active=False)
        return out
