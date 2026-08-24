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


@dataclass(frozen=True)
class ProviderRecord:
    provider_id: str
    model: str
    available: bool


class ProviderRegistry:
    """
    Snapshot provider metadata for the current process configuration.
    """

    def __init__(self, records: dict[str, ProviderRecord]):
        self._records = {
            provider_id: records[provider_id]
            for provider_id in PROVIDER_IDS
            if provider_id in records
        }

    @classmethod
    def from_env(cls):
        records = {}
        for provider_id, key_env, model_env in PROVIDER_ENV:
            api_key = os.getenv(key_env) or ""
            model = os.getenv(model_env) or ""
            records[provider_id] = ProviderRecord(
                provider_id=provider_id,
                model=model,
                available=bool(api_key) and bool(model),
            )
        return cls(records)

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
