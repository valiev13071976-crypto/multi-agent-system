import os
from dataclasses import dataclass

from side_effects.github.errors import GitHubWriteConfigError
from side_effects.github.models import GITHUB_NAME_RE


DEFAULT_TIMEOUT_SECONDS = 15.0
TOKEN_SECRET_NAME = "GITHUB_WRITE_TOKEN"


def _parse_bool(raw: str | None) -> bool:
    if raw is None or not str(raw).strip():
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def normalize_repository(value: str) -> str:
    text = str(value or "").strip()
    if not text or "*" in text or "?" in text or text.endswith("/"):
        raise GitHubWriteConfigError("github_repository_allowlist_invalid")
    if "://" in text or "/" not in text:
        raise GitHubWriteConfigError("github_repository_allowlist_invalid")
    parts = text.split("/")
    if len(parts) != 2:
        raise GitHubWriteConfigError("github_repository_allowlist_invalid")
    owner, repo = parts
    if not GITHUB_NAME_RE.match(owner) or not GITHUB_NAME_RE.match(repo):
        raise GitHubWriteConfigError("github_repository_allowlist_invalid")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise GitHubWriteConfigError("github_repository_allowlist_invalid")
    return f"{owner}/{repo}".lower()


def parse_allowed_repositories(raw: str | None) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        return ()
    seen = []
    for item in str(raw).split(","):
        if not item.strip():
            continue
        repo = normalize_repository(item)
        if repo not in seen:
            seen.append(repo)
    return tuple(seen)


@dataclass(frozen=True)
class GitHubWriteAdapterConfig:
    enabled: bool = False
    allowed_repositories: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self):
        timeout = float(self.timeout_seconds)
        if timeout <= 0:
            raise GitHubWriteConfigError("github_timeout_invalid")
        object.__setattr__(self, "timeout_seconds", timeout)
        normalized = []
        for repo in self.allowed_repositories:
            item = normalize_repository(repo)
            if item not in normalized:
                normalized.append(item)
        object.__setattr__(self, "allowed_repositories", tuple(normalized))

    def allows(self, owner: str, repo: str) -> bool:
        return f"{owner}/{repo}".lower() in set(self.allowed_repositories)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "GitHubWriteAdapterConfig":
        source = env if env is not None else os.environ
        enabled = _parse_bool(source.get("GITHUB_WRITE_ADAPTER_ENABLED"))
        timeout_raw = source.get("GITHUB_WRITE_TIMEOUT_SECONDS")
        timeout = DEFAULT_TIMEOUT_SECONDS
        if timeout_raw is not None and str(timeout_raw).strip():
            timeout = float(timeout_raw)
        return cls(
            enabled=enabled,
            allowed_repositories=parse_allowed_repositories(
                source.get("GITHUB_ALLOWED_REPOSITORIES")
            ),
            timeout_seconds=timeout,
        )

    def require_enabled(self) -> None:
        if not self.enabled:
            return
        if not self.allowed_repositories:
            raise GitHubWriteConfigError("github_allowlist_empty")
