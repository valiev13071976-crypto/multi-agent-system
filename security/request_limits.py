"""Central request size / abuse limits."""

from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from security.config import max_request_body_bytes


def _chat_upload_limit() -> int:
    return max(1024, int(os.environ.get("UI_CHAT_MAX_UPLOAD_BYTES") or str(10 * 1024 * 1024)))


def _effective_limit(path: str) -> int:
    if path.startswith("/api/chat/attachments") or path.startswith("/api/chat/voice/transcribe"):
        return _chat_upload_limit()
    if path.startswith("/api/v1/voice/"):
        return _chat_upload_limit()
    return max_request_body_bytes()


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies before handler execution."""

    async def dispatch(self, request: Request, call_next):
        limit = _effective_limit(request.url.path)
        raw = request.headers.get("content-length")
        if raw:
            try:
                if int(raw) > limit:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "payload_too_large"},
                    )
            except ValueError:
                pass
        return await call_next(request)
