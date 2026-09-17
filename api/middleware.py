"""
Middleware — request ID propagation and authentication.

Auth is controlled by AUTH_ENABLED setting:
  - false (default): pass through with dev UserContext
  - true: require valid Bearer JWT on every protected request

Token source: Authorization: Bearer <JWT>  (set by frontend after /auth/token)
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from core.auth import UserContext, parse_dev_user_context, verify_token
from core.errors import AuthenticationError
from core.settings import settings

logger = logging.getLogger(__name__)

# Paths that never require a token
_PUBLIC_PATHS = {
    "/health", "/docs", "/openapi.json", "/redoc",
    "/auth/token", "/auth/logout", "/auth/me",
}


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always allow public paths and static assets
        if (path in _PUBLIC_PATHS
                or path.startswith("/local-assets")
                or path.startswith("/auth/")):
            return await call_next(request)

        # ── Auth disabled (dev mode) ───────────────────────────────────────
        if not settings.auth_enabled:
            dev_header = request.headers.get("X-Dev-User-Context")
            request.state.user = parse_dev_user_context(dev_header)
            return await call_next(request)

        # ── Auth enabled — require Bearer token ────────────────────────────
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return Response(
                content='{"error":{"code":"MISSING_TOKEN","message":"Authorization: Bearer <token> required. POST /auth/token to login."}}',
                status_code=401,
                media_type="application/json",
            )

        token = auth_header.removeprefix("Bearer ").strip()
        try:
            request.state.user = verify_token(token)
        except AuthenticationError as exc:
            logger.warning("auth_failed", extra={"error": str(exc), "path": path})
            return Response(
                content=f'{{"error":{{"code":"INVALID_TOKEN","message":"{exc}"}}}}',
                status_code=401,
                media_type="application/json",
            )

        return await call_next(request)
