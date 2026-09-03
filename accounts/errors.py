"""Accounts domain errors — never embed secrets."""

from __future__ import annotations

from accounts.reasons import INVALID_CREDENTIALS


class AccountsError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class InvalidCredentialsError(AccountsError):
    def __init__(self):
        super().__init__(INVALID_CREDENTIALS, "Invalid username or password.")
