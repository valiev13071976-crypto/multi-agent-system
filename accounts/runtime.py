"""Accounts runtime wiring."""

from __future__ import annotations

import os
from dataclasses import dataclass

from accounts.service import AccountsService
from accounts.store import AccountsStore


@dataclass
class AccountsRuntime:
    service: AccountsService
    store: AccountsStore

    def close(self) -> None:
        self.store.close()


def build_accounts_runtime(*, saas_store=None, saas_billing=None, env: dict | None = None) -> AccountsRuntime:
    source = env if env is not None else os.environ
    db_path = source.get("ACCOUNTS_DB_PATH") or "data/accounts.sqlite"
    trial_raw = (source.get("PANDA_TRIAL_DAYS") or "").strip()
    trial_days = int(trial_raw) if trial_raw.isdigit() and int(trial_raw) > 0 else None
    secure = (source.get("PANDA_ENV") or source.get("ENVIRONMENT") or "").lower() in {"production", "prod"}
    store = AccountsStore(db_path)
    service = AccountsService(
        store=store,
        saas_store=saas_store,
        saas_billing=saas_billing,
        trial_days=trial_days,
        secure_cookies=secure,
    )
    return AccountsRuntime(service=service, store=store)
