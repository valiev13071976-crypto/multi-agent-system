"""Commercial deployment validation and readiness."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ReadinessCheck:
    name: str
    status: str
    detail: str = ""


@dataclass
class CommercialReadinessReport:
    overall: str
    checks: list[ReadinessCheck] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"overall": self.overall, "checks": [c.__dict__ for c in self.checks]}


def validate_production_config(*, env: dict | None = None) -> CommercialReadinessReport:
    source = env if env is not None else os.environ
    checks: list[ReadinessCheck] = []
    panda_env = (source.get("PANDA_ENV") or source.get("ENVIRONMENT") or "development").strip().lower()
    is_prod = panda_env in {"production", "prod"}

    auth_mode = (source.get("SECURITY_AUTH_MODE") or ("required" if is_prod else "disabled")).strip().lower()
    if is_prod and auth_mode != "required":
        checks.append(ReadinessCheck("auth", "FAIL", "Production requires SECURITY_AUTH_MODE=required"))
    else:
        checks.append(ReadinessCheck("auth", "PASS" if auth_mode == "required" or not is_prod else "WARN", auth_mode))

    billing_provider = (source.get("SAAS_BILLING_PROVIDER") or "fake").strip().lower()
    billing_enabled = (source.get("SAAS_BILLING_ENABLED") or "true").strip().lower() == "true"
    if is_prod and billing_enabled and billing_provider == "fake":
        checks.append(ReadinessCheck("billing_provider", "FAIL", "Fake billing forbidden in production when billing enabled"))
    else:
        checks.append(ReadinessCheck("billing_provider", "PASS", billing_provider))

    db_path = source.get("SAAS_PRODUCT_DB_PATH") or source.get("SIDE_EFFECT_DB_PATH") or ""
    if is_prod and not db_path:
        checks.append(ReadinessCheck("persistent_storage", "FAIL", "SAAS_PRODUCT_DB_PATH or SIDE_EFFECT_DB_PATH required"))
    else:
        checks.append(ReadinessCheck("persistent_storage", "PASS" if db_path else "WARN", db_path or "default"))

    if is_prod and not (source.get("PANDA_API_KEYS") or "").strip():
        checks.append(ReadinessCheck("api_keys", "FAIL", "PANDA_API_KEYS required in production"))
    else:
        checks.append(ReadinessCheck("api_keys", "PASS" if source.get("PANDA_API_KEYS") else "WARN", "configured" if source.get("PANDA_API_KEYS") else "missing"))

    public_url = (source.get("PUBLIC_URL") or source.get("PANDA_PUBLIC_URL") or "").strip()
    checks.append(ReadinessCheck("public_url", "PASS" if public_url or not is_prod else "WARN", public_url or "unset"))

    cors = (source.get("SECURITY_CORS_ORIGINS") or "").strip()
    if is_prod and cors == "*":
        checks.append(ReadinessCheck("cors", "FAIL", "Wildcard CORS not allowed in production"))
    else:
        checks.append(ReadinessCheck("cors", "PASS", cors or "default"))

    overall = "PASS"
    for c in checks:
        if c.status == "FAIL":
            overall = "FAIL"
            break
        if c.status == "WARN" and overall == "PASS":
            overall = "WARN"
    return CommercialReadinessReport(overall=overall, checks=checks)


def assert_production_safe(*, env: dict | None = None) -> None:
    report = validate_production_config(env=env)
    panda_env = ((env or os.environ).get("PANDA_ENV") or (env or os.environ).get("ENVIRONMENT") or "").strip().lower()
    if panda_env in {"production", "prod"} and report.overall == "FAIL":
        errors = [f"{c.name}:{c.detail}" for c in report.checks if c.status == "FAIL"]
        raise RuntimeError("production_config_invalid:" + ";".join(errors))
