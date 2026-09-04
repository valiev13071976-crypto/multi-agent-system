"""Telegram interface error taxonomy."""

from __future__ import annotations


class TelegramInterfaceError(Exception):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400):
        self.code = code
        self.message = message or code
        self.http_status = http_status
        super().__init__(self.message)


TGI_UNAUTHORIZED = "tgi_unauthorized"
TGI_BINDING_REQUIRED = "tgi_binding_required"
TGI_BINDING_REVOKED = "tgi_binding_revoked"
TGI_DUPLICATE_UPDATE = "tgi_duplicate_update"
TGI_NOT_FOUND = "tgi_not_found"
TGI_INVALID_CALLBACK = "tgi_invalid_callback"
TGI_CALLBACK_STALE = "tgi_callback_stale"
TGI_ACCESS_DENIED = "tgi_access_denied"
TGI_FILE_UNSUPPORTED = "tgi_file_unsupported"
TGI_FILE_TOO_LARGE = "tgi_file_too_large"
TGI_CONFIG_MISSING = "tgi_config_missing"
TGI_RATE_LIMITED = "tgi_rate_limited"
TGI_INVALID_UPDATE = "tgi_invalid_update"
TGI_UNKNOWN_USER = "tgi_unknown_user"
TGI_TENANT_MISMATCH = "tgi_tenant_mismatch"
TGI_USER_DISABLED = "tgi_user_disabled"
TGI_UNSUPPORTED_MESSAGE = "tgi_unsupported_message"
TGI_PAYLOAD_TOO_LARGE = "tgi_payload_too_large"
TGI_LIVE_FORBIDDEN = "tgi_live_forbidden"
TGI_RESPONSE_EMPTY = "tgi_response_empty"
TGI_PANDA_ERROR = "tgi_panda_error"
TGI_CAPABILITY_DENIED = "tgi_capability_denied"
TGI_APPROVAL_REQUIRED = "tgi_approval_required"
