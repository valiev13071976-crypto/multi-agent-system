from dataclasses import dataclass
import os


PROVIDER_IDS = (
    "openai",
    "anthropic",
    "gemini",
    "grok",
    "deepseek",
)

PROVIDER_ENV = (
    ("openai", "OPENAI_API_KEY", "OPENAI_MODEL"),
    ("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    ("gemini", "GEMINI_API_KEY", "GEMINI_MODEL"),
    ("grok", "XAI_API_KEY", "XAI_MODEL"),
    ("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
)

AUTO_PROVIDER_ORDER_ENV = "AUTO_PROVIDER_ORDER"

KNOWN_PROVIDER_IDS = frozenset(PROVIDER_IDS)


class InvalidAutoProviderOrderError(ValueError):
    def __init__(self, raw: str, unknown: tuple[str, ...]):
        self.raw = raw
        self.unknown = unknown
        unknown_list = ", ".join(unknown)
        super().__init__(
            f"Invalid {AUTO_PROVIDER_ORDER_ENV}={raw!r}: "
            f"unknown provider ids: {unknown_list}."
        )


def parse_auto_provider_order(raw: str | None) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        return PROVIDER_IDS

    ordered = []
    unknown = []
    seen = set()
    for part in str(raw).split(","):
        provider_id = part.strip()
        if not provider_id:
            continue
        if provider_id not in KNOWN_PROVIDER_IDS:
            if provider_id not in unknown:
                unknown.append(provider_id)
            continue
        if provider_id in seen:
            continue
        seen.add(provider_id)
        ordered.append(provider_id)

    if unknown:
        raise InvalidAutoProviderOrderError(raw=str(raw), unknown=tuple(unknown))

    if not ordered:
        return PROVIDER_IDS

    return tuple(ordered)


@dataclass(frozen=True)
class ProviderRecord:
    provider_id: str
    model: str
    available: bool


class ProviderRegistry:
    """
    Snapshot provider metadata for the current process configuration.
    """

    def __init__(
        self,
        records: dict[str, ProviderRecord],
        auto_provider_order: tuple[str, ...] | None = None,
        profiles: dict | None = None,
        auto_capability_fallback: str | None = None,
        auto_routing_policy: str | None = None,
    ):
        from agents.model_profile import (
            DEFAULT_AUTO_CAPABILITY_FALLBACK,
            DEFAULT_AUTO_ROUTING_POLICY,
            build_model_profile,
        )

        self._records = {
            provider_id: records[provider_id]
            for provider_id in PROVIDER_IDS
            if provider_id in records
        }
        self.auto_provider_order = (
            PROVIDER_IDS if auto_provider_order is None else tuple(auto_provider_order)
        )
        if profiles is None:
            self._profiles = {
                provider_id: build_model_profile(provider_id, record.model)
                for provider_id, record in self._records.items()
            }
        else:
            self._profiles = dict(profiles)
        self.auto_capability_fallback = (
            DEFAULT_AUTO_CAPABILITY_FALLBACK
            if auto_capability_fallback is None
            else auto_capability_fallback
        )
        self.auto_routing_policy = (
            DEFAULT_AUTO_ROUTING_POLICY
            if auto_routing_policy is None
            else auto_routing_policy
        )

    @classmethod
    def from_env(cls):
        from config.model_profiles import (
            load_auto_capability_fallback,
            load_auto_routing_policy,
            load_model_profiles,
        )

        records = {}
        for provider_id, key_env, model_env in PROVIDER_ENV:
            api_key = os.getenv(key_env) or ""
            model = os.getenv(model_env) or ""
            records[provider_id] = ProviderRecord(
                provider_id=provider_id,
                model=model,
                available=bool(api_key) and bool(model),
            )
        return cls(
            records,
            auto_provider_order=parse_auto_provider_order(
                os.getenv(AUTO_PROVIDER_ORDER_ENV)
            ),
            profiles=load_model_profiles(records),
            auto_capability_fallback=load_auto_capability_fallback(),
            auto_routing_policy=load_auto_routing_policy(),
        )

    def is_available(self, provider_id: str) -> bool:
        record = self._records.get(provider_id)
        return bool(record and record.available)

    def model(self, provider_id: str) -> str:
        record = self._records.get(provider_id)
        if record is None:
            return ""
        return record.model

    def available_provider_ids(self) -> tuple[str, ...]:
        return tuple(
            provider_id
            for provider_id in PROVIDER_IDS
            if self.is_available(provider_id)
        )

    def status(self) -> dict[str, bool]:
        return {
            provider_id: self.is_available(provider_id)
            for provider_id in PROVIDER_IDS
        }

    def profile(self, provider_id: str):
        return self._profiles.get(provider_id)

    def is_active_profile(self, provider_id: str) -> bool:
        if not self.is_available(provider_id):
            return False
        profile = self.profile(provider_id)
        if profile is None or not profile.enabled:
            return False
        return profile.model_id == self.model(provider_id)

    def active_provider_ids(self) -> tuple[str, ...]:
        return tuple(
            provider_id
            for provider_id in self.auto_provider_order
            if self.is_active_profile(provider_id)
        )

    def providers_supporting(self, category: str) -> tuple[str, ...]:
        matching = []
        for provider_id in self.active_provider_ids():
            profile = self.profile(provider_id)
            if profile and category in profile.task_categories:
                matching.append(provider_id)
        return tuple(matching)
