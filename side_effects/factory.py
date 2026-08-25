from security.secrets import SecretStore
from side_effects.github.adapter import GitHubIssueLabelAdapter
from side_effects.github.config import GitHubWriteAdapterConfig, TOKEN_SECRET_NAME
from side_effects.github.errors import GitHubWriteConfigError
from side_effects.github.transport import GitHubHttpTransport
from side_effects.registry import empty_adapter_registry


def build_production_side_effect_registry(
    *,
    secrets: SecretStore,
    config: GitHubWriteAdapterConfig | None = None,
    env: dict | None = None,
):
    """Register github.issue_labels only when fully configured. Fail closed if enabled but invalid."""

    registry = empty_adapter_registry()
    cfg = config if config is not None else GitHubWriteAdapterConfig.from_env(env)
    if not cfg.enabled:
        return registry
    if not cfg.allowed_repositories:
        raise GitHubWriteConfigError("github_allowlist_empty")
    token = secrets.get(TOKEN_SECRET_NAME)
    if token is None or not str(token).strip():
        raise GitHubWriteConfigError("github_token_missing")
    transport = GitHubHttpTransport(token, timeout_seconds=cfg.timeout_seconds)
    registry.register(GitHubIssueLabelAdapter(config=cfg, transport=transport))
    return registry
