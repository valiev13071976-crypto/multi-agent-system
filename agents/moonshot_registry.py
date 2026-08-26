"""Versioned Moonshot / Kimi model registry profiles (config-driven selection)."""

from __future__ import annotations

from dataclasses import dataclass

from agents.moonshot_versions import (
    MODEL_STATE_ACTIVE,
    MODEL_STATE_DEPRECATED,
    MODEL_STATE_DISABLED,
    MODEL_STATES,
    MOONSHOT_MODEL_REGISTRY_VERSION,
    MOONSHOT_PROVIDER_ID,
    QUALITY_STATUS_PROVISIONAL,
    QUALITY_STATUS_VERIFIED,
    QUALITY_STATUSES,
)


@dataclass(frozen=True)
class MoonshotModelProfile:
    """Capability profile for one configured Moonshot model id."""

    model_id: str
    state: str = MODEL_STATE_ACTIVE
    quality_status: str = QUALITY_STATUS_PROVISIONAL
    quality_class: str = "standard"
    cost_class: str = "standard"
    latency_class: str = "standard"
    context_class: str = "long"
    context_window: int | None = None
    text_generation: bool = True
    reasoning: bool = False
    long_context: bool = True
    multilingual: bool = True
    structured_output: bool = False
    tool_calling: bool = False
    vision: bool = False
    coding: bool = False
    registry_version: str = MOONSHOT_MODEL_REGISTRY_VERSION

    def __post_init__(self):
        if self.state not in MODEL_STATES:
            object.__setattr__(self, "state", MODEL_STATE_DISABLED)
        if self.quality_status not in QUALITY_STATUSES:
            object.__setattr__(self, "quality_status", QUALITY_STATUS_PROVISIONAL)

    @property
    def selectable(self) -> bool:
        return self.state == MODEL_STATE_ACTIVE

    def as_dict(self) -> dict:
        return {
            "provider_id": MOONSHOT_PROVIDER_ID,
            "model_id": self.model_id,
            "state": self.state,
            "quality_status": self.quality_status,
            "quality_class": self.quality_class,
            "cost_class": self.cost_class,
            "latency_class": self.latency_class,
            "context_class": self.context_class,
            "context_window": self.context_window,
            "capabilities": {
                "text_generation": self.text_generation,
                "reasoning": self.reasoning,
                "long_context": self.long_context,
                "multilingual": self.multilingual,
                "structured_output": self.structured_output,
                "tool_calling": self.tool_calling,
                "vision": self.vision,
                "coding": self.coding,
            },
            "registry_version": self.registry_version,
        }


def build_moonshot_profile_from_env(
    model_id: str,
    *,
    state: str = MODEL_STATE_ACTIVE,
    quality_status: str = QUALITY_STATUS_PROVISIONAL,
    quality_class: str = "standard",
    cost_class: str = "standard",
    latency_class: str = "standard",
    context_class: str = "long",
    context_window: int | None = None,
    structured_output: bool = False,
    tool_calling: bool = False,
    vision: bool = False,
    reasoning: bool = False,
    coding: bool = False,
) -> MoonshotModelProfile:
    """Build profile for the configured model. Capabilities must be explicit (no family-wide assume)."""

    mid = str(model_id or "").strip()
    long_ctx = context_class == "long" or (context_window is not None and int(context_window) >= 100_000)
    return MoonshotModelProfile(
        model_id=mid,
        state=state if state in MODEL_STATES else MODEL_STATE_DISABLED,
        quality_status=(
            quality_status if quality_status in QUALITY_STATUSES else QUALITY_STATUS_PROVISIONAL
        ),
        quality_class=quality_class,
        cost_class=cost_class,
        latency_class=latency_class,
        context_class=context_class,
        context_window=context_window,
        reasoning=bool(reasoning),
        long_context=bool(long_ctx),
        structured_output=bool(structured_output),
        tool_calling=bool(tool_calling),
        vision=bool(vision),
        coding=bool(coding),
    )


def moonshot_model_registry_snapshot() -> dict:
    return {
        "moonshot_model_registry_version": MOONSHOT_MODEL_REGISTRY_VERSION,
        "provider_id": MOONSHOT_PROVIDER_ID,
        "model_ids_config_driven": True,
        "no_eternal_default_model": True,
        "quality_default": QUALITY_STATUS_PROVISIONAL,
        "pricing_default": "unknown",
        "states": list(MODEL_STATES),
        "deprecated_not_auto_selected": True,
    }
