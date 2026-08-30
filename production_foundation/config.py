"""Centralized production configuration contract."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from production_foundation.errors import PF_CONFIG_INVALID, ProductionFoundationError
from security.config import AUTH_MODE_REQUIRED, security_auth_mode

ENV_DEVELOPMENT = "development"
ENV_TEST = "test"
ENV_PRODUCTION = "production"

PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "test-secret",
        "secret",
        "development-only",
        "dev-secret",
        "placeholder",
    }
)

EPHEMERAL_PATH_MARKERS = ("/tmp/", "/var/tmp/", "/appdata/local/temp/")


def _is_ephemeral_production_path(path: str) -> bool:
    lower = str(Path(path).resolve()).replace("\\", "/").lower()
    return any(marker in lower for marker in EPHEMERAL_PATH_MARKERS)


@dataclass
class ConfigCheck:
    name: str
    status: str
    detail: str = ""


@dataclass
class ProductionConfigReport:
    environment: str
    overall: str
    checks: list[ConfigCheck] = field(default_factory=list)
    summary: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "environment": self.environment,
            "overall": self.overall,
            "checks": [c.__dict__ for c in self.checks],
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class ProductionConfig:
    environment: str
    data_dir: str
    side_effect_db_path: str
    saas_db_path: str
    ops_admin_db_path: str
    artifact_root: str
    backup_root: str
    export_root: str
    public_url: str
    auth_mode: str
    billing_enabled: bool
    billing_provider: str
    backup_destination: str
    alert_webhook_url: str
    disk_free_threshold_bytes: int
    backup_stale_hours: int
    process_role: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def resolve_environment(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    raw = (source.get("PANDA_ENV") or source.get("ENVIRONMENT") or ENV_DEVELOPMENT).strip().lower()
    if raw in {ENV_PRODUCTION, "prod"}:
        return ENV_PRODUCTION
    if raw in {ENV_TEST, "testing", "ci"}:
        return ENV_TEST
    if raw in {ENV_DEVELOPMENT, "dev", "local"}:
        return ENV_DEVELOPMENT
    return raw


def is_production(env: dict | None = None) -> bool:
    return resolve_environment(env) == ENV_PRODUCTION


def _join_data_dir(data_dir: str, *parts: str) -> str:
    root = data_dir.rstrip("/\\")
    return os.path.join(root, *parts)


def resolve_production_config(env: dict | None = None, *, raise_on_error: bool = False) -> ProductionConfig:
    source = env if env is not None else os.environ
    environment = resolve_environment(source)
    is_prod = environment == ENV_PRODUCTION

    data_dir = (source.get("PANDA_DATA_DIR") or source.get("DATA_DIR") or "").strip()
    if not data_dir:
        data_dir = "/data" if is_prod else "./data"

    side_db = (source.get("SIDE_EFFECT_DB_PATH") or "").strip()
    if not side_db:
        side_db = _join_data_dir(data_dir, "side_effects.sqlite3")

    saas_db = (source.get("SAAS_PRODUCT_DB_PATH") or "").strip()
    if not saas_db:
        saas_db = _join_data_dir(data_dir, "saas_product.sqlite")

    ops_db = (source.get("OPS_ADMIN_DB_PATH") or "").strip()
    if not ops_db:
        ops_db = _join_data_dir(data_dir, "ops_admin.sqlite")

    artifact_root = (source.get("PANDA_ARTIFACT_ROOT") or source.get("ARTIFACT_ROOT") or "").strip()
    if not artifact_root:
        artifact_root = _join_data_dir(data_dir, "artifacts")

    backup_root = (source.get("PANDA_BACKUP_ROOT") or "").strip()
    if not backup_root:
        backup_root = _join_data_dir(data_dir, "backups")

    export_root = (source.get("SAAS_EXPORT_ROOT") or "").strip()
    if not export_root:
        export_root = _join_data_dir(data_dir, "privacy_exports")

    public_url = (source.get("PUBLIC_URL") or source.get("PANDA_PUBLIC_URL") or "").strip()
    auth_mode = security_auth_mode(source)
    billing_enabled = (source.get("SAAS_BILLING_ENABLED") or "true").strip().lower() == "true"
    billing_provider = (source.get("SAAS_BILLING_PROVIDER") or "fake").strip().lower()
    backup_destination = (source.get("PANDA_BACKUP_DESTINATION") or "local").strip().lower()
    alert_webhook = (source.get("PANDA_ALERT_WEBHOOK_URL") or "").strip()
    process_role = (source.get("RUNTIME_ROLE") or "combined").strip().lower()

    try:
        disk_threshold = int(source.get("PANDA_DISK_FREE_THRESHOLD_BYTES") or str(100 * 1024 * 1024))
    except ValueError:
        disk_threshold = 100 * 1024 * 1024

    try:
        backup_stale_hours = int(source.get("PANDA_BACKUP_STALE_HOURS") or "26")
    except ValueError:
        backup_stale_hours = 26

    errors: list[str] = []
    warnings: list[str] = []

    if environment not in {ENV_DEVELOPMENT, ENV_TEST, ENV_PRODUCTION}:
        errors.append(f"unknown_environment:{environment}")

    if is_prod:
        if auth_mode != AUTH_MODE_REQUIRED:
            errors.append("production_auth_not_required")
        if not (source.get("PANDA_API_KEYS") or "").strip():
            errors.append("production_api_keys_missing")
        if billing_enabled and billing_provider == "fake":
            errors.append("production_fake_billing_forbidden")
        if not public_url:
            errors.append("production_public_url_missing")
        elif public_url.startswith("http://localhost") or "127.0.0.1" in public_url:
            errors.append("production_localhost_public_url")
        cors = (source.get("SECURITY_CORS_ORIGINS") or "").strip()
        if cors == "*":
            errors.append("production_wildcard_cors")

        for label, path in (
            ("side_effect_db", side_db),
            ("saas_db", saas_db),
            ("artifact_root", artifact_root),
        ):
            if _is_ephemeral_production_path(path):
                errors.append(f"production_ephemeral_{label}")

        if _is_ephemeral_production_path(data_dir):
            errors.append("production_ephemeral_data_dir")

        secret = (source.get("PANDA_CAPABILITY_SIGNING_KEY") or source.get("SECURITY_SESSION_SECRET") or "").strip()
        if secret and secret.lower() in PLACEHOLDER_SECRETS:
            errors.append("production_placeholder_secret")

    cfg = ProductionConfig(
        environment=environment,
        data_dir=data_dir,
        side_effect_db_path=side_db,
        saas_db_path=saas_db,
        ops_admin_db_path=ops_db,
        artifact_root=artifact_root,
        backup_root=backup_root,
        export_root=export_root,
        public_url=public_url,
        auth_mode=auth_mode,
        billing_enabled=billing_enabled,
        billing_provider=billing_provider,
        backup_destination=backup_destination,
        alert_webhook_url=alert_webhook,
        disk_free_threshold_bytes=max(1, disk_threshold),
        backup_stale_hours=max(1, backup_stale_hours),
        process_role=process_role,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
    if errors and raise_on_error:
        raise ProductionFoundationError(PF_CONFIG_INVALID, ";".join(errors))
    return cfg


def validate_production_config(env: dict | None = None) -> ProductionConfigReport:
    cfg = resolve_production_config(env)
    checks: list[ConfigCheck] = []
    is_prod = cfg.environment == ENV_PRODUCTION

    checks.append(ConfigCheck("environment", "PASS", cfg.environment))
    checks.append(ConfigCheck("auth", "PASS" if cfg.auth_mode == AUTH_MODE_REQUIRED or not is_prod else "FAIL", cfg.auth_mode))
    checks.append(
        ConfigCheck(
            "billing_provider",
            "FAIL" if is_prod and cfg.billing_enabled and cfg.billing_provider == "fake" else "PASS",
            cfg.billing_provider,
        )
    )
    checks.append(ConfigCheck("storage_root", "PASS" if cfg.data_dir else "FAIL", cfg.data_dir))
    checks.append(ConfigCheck("public_url", "PASS" if cfg.public_url or not is_prod else "FAIL", cfg.public_url or "unset"))
    checks.append(
        ConfigCheck(
            "backup_destination",
            "WARN" if cfg.backup_destination == "local" and is_prod else "PASS",
            cfg.backup_destination,
        )
    )
    checks.append(
        ConfigCheck(
            "alert_sink",
            "WARN" if not cfg.alert_webhook_url else "PASS",
            "configured" if cfg.alert_webhook_url else "not_configured",
        )
    )

    for err in cfg.errors:
        checks.append(ConfigCheck(err.split(":")[0], "FAIL", err))

    overall = "PASS"
    for c in checks:
        if c.status == "FAIL":
            overall = "FAIL"
            break
        if c.status == "WARN" and overall == "PASS":
            overall = "WARN"

    summary = {
        "environment": cfg.environment,
        "storage": "persistent" if cfg.data_dir else "unknown",
        "billing": cfg.billing_provider if cfg.billing_enabled else "disabled",
        "auth": cfg.auth_mode,
        "alerts": "configured" if cfg.alert_webhook_url else "not_configured",
        "public_url": "configured" if cfg.public_url else "unset",
        "backup": cfg.backup_destination,
    }
    return ProductionConfigReport(environment=cfg.environment, overall=overall, checks=checks, summary=summary)


def assert_production_startup_safe(env: dict | None = None) -> None:
    cfg = resolve_production_config(env, raise_on_error=False)
    if cfg.environment == ENV_PRODUCTION and cfg.errors:
        raise ProductionFoundationError(PF_CONFIG_INVALID, ";".join(cfg.errors))


def is_valid_public_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def reject_placeholder_secret(value: str) -> bool:
    return value.strip().lower() not in PLACEHOLDER_SECRETS
