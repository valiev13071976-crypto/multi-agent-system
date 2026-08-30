"""Launch provider manifest."""

from __future__ import annotations

from dataclasses import dataclass, field

from production_activation.models import ProviderRequirement, VerificationClass


@dataclass
class ProviderManifestEntry:
    provider_id: str
    requirement: str
    status: str = "unknown"
    evidence_class: str = VerificationClass.NOT_APPLICABLE.value
    healthy: bool = False

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "requirement": self.requirement,
            "status": self.status,
            "evidence_class": self.evidence_class,
            "healthy": self.healthy,
        }


@dataclass
class ProviderManifest:
    entries: list[ProviderManifestEntry] = field(default_factory=list)

    @classmethod
    def from_plan(cls, required: tuple[str, ...], *, optional: tuple[str, ...] = ()) -> ProviderManifest:
        entries = [
            ProviderManifestEntry(provider_id=p, requirement=ProviderRequirement.REQUIRED.value)
            for p in required
        ]
        entries.extend(
            ProviderManifestEntry(provider_id=p, requirement=ProviderRequirement.OPTIONAL.value)
            for p in optional
        )
        return cls(entries=entries)

    def record_live(self, provider_id: str, *, healthy: bool = True) -> None:
        for entry in self.entries:
            if entry.provider_id == provider_id:
                entry.status = "live"
                entry.evidence_class = VerificationClass.LIVE_VERIFIED.value
                entry.healthy = healthy

    def record_not_enabled(self, provider_id: str) -> None:
        for entry in self.entries:
            if entry.provider_id == provider_id:
                entry.status = "not_enabled"
                entry.evidence_class = VerificationClass.NOT_ENABLED.value

    def blocks_acceptance(self) -> bool:
        for entry in self.entries:
            if entry.requirement == ProviderRequirement.REQUIRED.value and entry.evidence_class != VerificationClass.LIVE_VERIFIED.value:
                return True
        return False

    def as_dict(self) -> dict:
        return {"providers": [e.as_dict() for e in self.entries]}
