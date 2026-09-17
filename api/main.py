"""
FastAPI application entry point.

Startup order:
  1. configure_langsmith()  — must run before any LangChain/LangGraph imports
  2. configure_tracing()    — OpenTelemetry setup
  3. App + middleware + routers
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.observability import configure_langsmith, configure_tracing
configure_langsmith()
configure_tracing()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.middleware import AuthMiddleware, RequestIDMiddleware
from api.v1 import admin, assets, auth, chat, share
from core.errors import (  
    AuthenticationError,
    AuthorizationError,
    DanubeError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from core.settings import settings

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Danube AI Chatbot",
    version="1.0.0",
    docs_url="/docs" if settings.environment.value != "production" else None,
    redoc_url="/redoc" if settings.environment.value != "production" else None,
)


# ── Middleware — registered outermost first ───────────────────────────────────
app.add_middleware(RequestIDMiddleware)
app.add_middleware(AuthMiddleware)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "https://promotions.danubeproperties.com"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)                                          # /auth/*
app.include_router(chat.router,   prefix="/api/v1/chat",   tags=["chat"])
app.include_router(assets.router, prefix="/api/v1/assets", tags=["assets"])
app.include_router(share.router,  prefix="/api/v1",        tags=["feedback"])
app.include_router(admin.router,  prefix="/api/v1/admin",  tags=["admin"])

# ── Local storage static mount ────────────────────────────────────────────────
if settings.environment.value == "local":
    import os
    os.makedirs(settings.local_storage_path, exist_ok=True)
    app.mount(
        "/local-assets",
        StaticFiles(directory=settings.local_storage_path),
        name="local_assets",
    )

# ── Dev test UI ───────────────────────────────────────────────────────────────
ui_directory = Path(__file__).resolve().parents[1] / "static"
app.mount(
    "/ui",
    StaticFiles(directory=ui_directory, html=True),
    name="ui",
)

# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": str(exc)}})


@app.exception_handler(AuthenticationError)
async def auth_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": {"code": "UNAUTHORIZED", "message": "Authentication failed."}})


@app.exception_handler(AuthorizationError)
async def authz_handler(request: Request, exc: AuthorizationError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": {"code": "FORBIDDEN", "message": "Access denied."}})


@app.exception_handler(ValidationError)
async def validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": str(exc)}})


@app.exception_handler(RateLimitError)
async def rate_limit_handler(request: Request, exc: RateLimitError) -> JSONResponse:
    return JSONResponse(status_code=429, content={"error": {"code": "RATE_LIMITED", "message": "Too many requests."}})


@app.exception_handler(DanubeError)
async def danube_error_handler(request: Request, exc: DanubeError) -> JSONResponse:
    logger.error("unhandled_danube_error", extra={"error": str(exc)})
    return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}})


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", extra={"error": str(exc), "type": type(exc).__name__})
    return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}})


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    logger.info("danube_ai_startup", extra={"environment": settings.environment.value})


@app.on_event("shutdown")
async def shutdown() -> None:
    logger.info("danube_ai_shutdown")


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.environment.value}

