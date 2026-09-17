"""
OAuth2 Password Grant endpoints.

POST /auth/token   — exchange username+password for a JWT access token
GET  /auth/me      — return current user info
GET  /auth/logout  — instruct client to discard token (stateless, informational)

Standard OAuth2 token response:
  {
    "access_token": "<JWT>",
    "token_type":   "bearer",
    "expires_in":   7200
  }

Frontend usage:
  const { access_token } = await POST /auth/token (form-encoded)
  sessionStorage.setItem("danube_token", access_token)
  fetch("/api/v1/...", { headers: { Authorization: `Bearer ${access_token}` } })
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from core.auth import UserContext, authenticate_user, create_access_token, verify_token
from core.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


# ── POST /auth/token ──────────────────────────────────────────────────────────

@router.post("/auth/token")
async def token(form: OAuth2PasswordRequestForm = Depends()):
    """
    OAuth2 Password Grant.
    Accepts application/x-www-form-urlencoded with fields: username, password.
    Returns a bearer token valid for settings.token_ttl_seconds.
    """
    user = authenticate_user(form.username, form.password)
    if not user:
        logger.warning("login_failed", extra={"username": form.username})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(username=form.username, role=user["role"])
    logger.info("login_success", extra={"username": form.username, "role": user["role"]})

    return {
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   settings.token_ttl_seconds,
        "role":         user["role"],
        "display_name": form.username,
    }


# ── GET /auth/me ──────────────────────────────────────────────────────────────

@router.get("/auth/me")
async def me(request: Request):
    """Return the current authenticated user. 401 if not logged in."""
    user: UserContext | None = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return {
        "broker_id":    user.broker_id,
        "display_name": user.display_name,
        "email":        user.email,
        "role":         user.role,
        "org_id":       user.org_id,
        "tenant_id":    user.tenant_id,
    }


# ── GET /auth/logout ──────────────────────────────────────────────────────────

@router.get("/auth/logout")
async def logout():
    """
    Stateless logout — JWTs cannot be revoked server-side.
    Client must discard the token from sessionStorage on receipt of this response.
    """
    return {"message": "Logged out. Discard your access token."}

