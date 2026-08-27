"""Authentication strategy abstraction — server-side secret injection only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol
from urllib.parse import urlencode, urlparse, urlunparse

from integrations.contracts import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_BEARER,
    AUTH_OAUTH2,
    AUTH_SERVICE_ACCOUNT,
    AUTH_SIGNED,
    OAuthTokenBundle,
)
from integrations.errors import (
    AuthenticationFailedError,
    CredentialExpiredError,
    CredentialMissingError,
)


@dataclass(frozen=True)
class AuthMaterial:
    """Headers/query prepared for a single outbound call — ephemeral."""

    headers: Mapping[str, str]
    query: Mapping[str, str] = field(default_factory=dict)
    allow_query_secret: bool = False

    def __post_init__(self):
        object.__setattr__(self, "headers", dict(self.headers or {}))
        object.__setattr__(self, "query", dict(self.query or {}))


class IntegrationAuthStrategy(Protocol):
    strategy_id: str

    def build_auth(
        self,
        *,
        secret: str,
        settings: Mapping[str, object] | None = None,
    ) -> AuthMaterial:
        ...


class ApiKeyAuthStrategy:
    strategy_id = AUTH_API_KEY

    def build_auth(
        self,
        *,
        secret: str,
        settings: Mapping[str, object] | None = None,
    ) -> AuthMaterial:
        if not secret:
            raise CredentialMissingError("credential_missing")
        cfg = dict(settings or {})
        placement = str(cfg.get("api_key_placement") or "header").lower()
        header_name = str(cfg.get("api_key_header") or "Authorization")
        prefix = str(cfg.get("api_key_prefix") or "Bearer ")
        if placement == "query":
            # Discouraged — explicit opt-in only
            if not bool(cfg.get("allow_query_api_key")):
                raise AuthenticationFailedError("authentication_failed")
            param = str(cfg.get("api_key_query_param") or "api_key")
            return AuthMaterial(headers={}, query={param: secret}, allow_query_secret=True)
        if placement == "custom_header":
            return AuthMaterial(headers={header_name: secret})
        # default Authorization header
        value = f"{prefix}{secret}" if prefix else secret
        return AuthMaterial(headers={header_name: value})


class BearerAuthStrategy:
    strategy_id = AUTH_BEARER

    def build_auth(
        self,
        *,
        secret: str,
        settings: Mapping[str, object] | None = None,
    ) -> AuthMaterial:
        if not secret:
            raise CredentialMissingError("credential_missing")
        return AuthMaterial(headers={"Authorization": f"Bearer {secret}"})


class BasicAuthStrategy:
    strategy_id = AUTH_BASIC

    def build_auth(
        self,
        *,
        secret: str,
        settings: Mapping[str, object] | None = None,
    ) -> AuthMaterial:
        import base64

        cfg = dict(settings or {})
        if not bool(cfg.get("allow_basic_auth")):
            raise AuthenticationFailedError("authentication_failed")
        username = str(cfg.get("username") or "")
        if not username or not secret:
            raise CredentialMissingError("credential_missing")
        token = base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("ascii")
        return AuthMaterial(headers={"Authorization": f"Basic {token}"})


class OAuth2AuthStrategy:
    """OAuth2 access/refresh foundation — refresh via injectable callback."""

    strategy_id = AUTH_OAUTH2

    def __init__(
        self,
        *,
        refresh_fn: Callable[[str, Mapping[str, object]], OAuthTokenBundle] | None = None,
    ):
        self._refresh_fn = refresh_fn
        self._cache: dict[str, OAuthTokenBundle] = {}

    def build_auth(
        self,
        *,
        secret: str,
        settings: Mapping[str, object] | None = None,
    ) -> AuthMaterial:
        # `secret` is access token; refresh handled separately
        if not secret:
            raise CredentialMissingError("credential_missing")
        return AuthMaterial(headers={"Authorization": f"Bearer {secret}"})

    def ensure_access_token(
        self,
        *,
        cache_key: str,
        access_token: str,
        refresh_token: str = "",
        expires_at: datetime | None = None,
        scopes: tuple[str, ...] = (),
        settings: Mapping[str, object] | None = None,
    ) -> str:
        bundle = OAuthTokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
        )
        if not bundle.expired():
            self._cache[cache_key] = bundle
            return bundle.access_token
        if not refresh_token or self._refresh_fn is None:
            raise CredentialExpiredError("credential_expired")
        refreshed = self._refresh_fn(refresh_token, dict(settings or {}))
        if not refreshed.access_token:
            raise AuthenticationFailedError("authentication_failed")
        self._cache[cache_key] = refreshed
        return refreshed.access_token


class ServiceAccountAuthStrategy:
    """Service-account foundation — signing/token exchange via hook."""

    strategy_id = AUTH_SERVICE_ACCOUNT

    def __init__(
        self,
        *,
        exchange_fn: Callable[[str, Mapping[str, object]], str] | None = None,
    ):
        self._exchange_fn = exchange_fn
        self._token_cache: dict[str, tuple[str, datetime | None]] = {}

    def build_auth(
        self,
        *,
        secret: str,
        settings: Mapping[str, object] | None = None,
    ) -> AuthMaterial:
        cfg = dict(settings or {})
        cache_key = str(cfg.get("cache_key") or "default")
        cached = self._token_cache.get(cache_key)
        now = datetime.now(timezone.utc)
        if cached and (cached[1] is None or cached[1] > now):
            return AuthMaterial(headers={"Authorization": f"Bearer {cached[0]}"})
        if self._exchange_fn is None:
            # Direct use of short-lived token material when exchange not configured
            if not secret:
                raise CredentialMissingError("credential_missing")
            return AuthMaterial(headers={"Authorization": f"Bearer {secret}"})
        token = self._exchange_fn(secret, cfg)
        if not token:
            raise AuthenticationFailedError("authentication_failed")
        ttl = float(cfg.get("token_ttl_seconds") or 300)
        expires = datetime.fromtimestamp(now.timestamp() + ttl, tz=timezone.utc)
        self._token_cache[cache_key] = (token, expires)
        return AuthMaterial(headers={"Authorization": f"Bearer {token}"})


class SignedRequestAuthStrategy:
    """Provider-specific signed request extension point."""

    strategy_id = AUTH_SIGNED

    def __init__(self, *, signer: Callable[[str, Mapping[str, object]], AuthMaterial] | None = None):
        self._signer = signer

    def build_auth(
        self,
        *,
        secret: str,
        settings: Mapping[str, object] | None = None,
    ) -> AuthMaterial:
        if self._signer is None:
            raise AuthenticationFailedError("authentication_failed")
        return self._signer(secret, dict(settings or {}))


STRATEGIES: dict[str, IntegrationAuthStrategy] = {
    AUTH_API_KEY: ApiKeyAuthStrategy(),
    AUTH_BEARER: BearerAuthStrategy(),
    AUTH_BASIC: BasicAuthStrategy(),
    AUTH_OAUTH2: OAuth2AuthStrategy(),
    AUTH_SERVICE_ACCOUNT: ServiceAccountAuthStrategy(),
    AUTH_SIGNED: SignedRequestAuthStrategy(),
}


def get_auth_strategy(strategy_id: str) -> IntegrationAuthStrategy:
    strat = STRATEGIES.get(strategy_id)
    if strat is None:
        raise AuthenticationFailedError("authentication_failed")
    return strat


def apply_auth_to_url(url: str, material: AuthMaterial) -> str:
    """Attach query auth only when explicitly allowed."""
    if not material.query:
        return url
    if not material.allow_query_secret:
        raise AuthenticationFailedError("authentication_failed")
    parsed = urlparse(url)
    from urllib.parse import parse_qsl

    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q.update(material.query)
    return urlunparse(parsed._replace(query=urlencode(q)))
