"""Central request size / abuse limits."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from security.config import max_request_body_bytes


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies before handler execution."""

    async def dispatch(self, request: Request, call_next):
        limit = max_request_body_bytes()
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
