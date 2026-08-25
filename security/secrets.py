import os
from typing import Protocol


SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "PANDA_ENCRYPTION_KEY",
)


class SecretStore(Protocol):
    def get(self, name: str) -> str | None:
        ...


class EnvSecretStore:
    """
    Compatibility backend: current process environment.
    Production can replace this with an external secret manager.
    """

    def get(self, name: str) -> str | None:
        value = os.getenv(name)
        if value is None or not str(value).strip():
            return None
        return value


class SecretProvider:
    def __init__(self, store: SecretStore | None = None):
        self._store = store or EnvSecretStore()

    def get(self, name: str) -> str | None:
        return self._store.get(name)

    def known_secret_values(self) -> tuple[str, ...]:
        values = []
        for name in SECRET_ENV_NAMES:
            value = self._store.get(name)
            if value:
                values.append(value)
        return tuple(values)
