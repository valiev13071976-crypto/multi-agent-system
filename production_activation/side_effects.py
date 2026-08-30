"""Billing and side-effect activation policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from production_activation.errors import AUTHORIZATION_DENIED, ProductionActivationError
from production_activation.models import SideEffectMode


@dataclass
class SideEffectManifestEntry:
    integration: str
    mode: str
    activated: bool = False
    live: bool = False

    def as_dict(self) -> dict:
        return {"integration": self.integration, "mode": self.mode, "activated": self.activated, "live": self.live}


@dataclass
class SideEffectActivationPolicy:
    """Production traffic does NOT auto-enable external writes."""

    entries: list[SideEffectManifestEntry] = field(default_factory=list)
    billing_mode: str = "sandbox"
    billing_live: bool = False

    @classmethod
    def from_plan(cls, policy: dict[str, str], *, billing_mode: str) -> SideEffectActivationPolicy:
        entries = [SideEffectManifestEntry(integration=k, mode=v) for k, v in policy.items()]
        return cls(entries=entries, billing_mode=billing_mode, billing_live=billing_mode == "live")

    def activate_billing_live(self, *, authorized: bool, operator_ref: str) -> None:
        if not authorized:
            raise ProductionActivationError(AUTHORIZATION_DENIED, details={"billing": "live_requires_authorization"})
        self.billing_mode = "live"
        self.billing_live = True
        for entry in self.entries:
            if entry.integration == "billing":
                entry.mode = SideEffectMode.REQUIRED_LIVE.value
                entry.activated = True
                entry.live = True

    def traffic_activation_side_effects(self) -> dict[str, str]:
        """Returns current modes — traffic activation alone does not flip writes."""
        return {e.integration: e.mode for e in self.entries}

    def blocks_unauthorized_live(self) -> bool:
        for entry in self.entries:
            if entry.live and entry.mode == SideEffectMode.REQUIRED_LIVE.value and not entry.activated:
                return True
        return False

    def as_dict(self) -> dict:
        return {
            "billing_mode": self.billing_mode,
            "billing_live": self.billing_live,
            "entries": [e.as_dict() for e in self.entries],
        }
