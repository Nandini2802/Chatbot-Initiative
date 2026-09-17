"""
Auth — UserContext, OAuth2 Password Grant (backend-issued JWTs), RBAC.

Flow:
  POST /auth/token  {username, password}
       → backend validates against DANUBE_USERS config
       → returns {access_token, token_type, expires_in}
       → frontend stores token in sessionStorage
       → all API requests: Authorization: Bearer <token>

Token format: HS256 JWT signed with JWT_SECRET.
Salesforce integration: deferred — add /auth/callback flow when SF Connected App is ready.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

from jose import JWTError, jwt

from core.errors import AuthenticationError, AuthorizationError
from core.settings import settings

logger = logging.getLogger(__name__)


# ── UserContext ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UserContext:
    broker_id: str
    tenant_id: str
    role: str          # "broker" | "rm" | "admin"
    session_id: str
    display_name: str
    email: str
    org_id: str
    extra: dict = field(default_factory=dict)

    def can_access_project(self, project_id: str) -> bool:
        return self.role in ("broker", "rm", "admin")

    def is_admin(self) -> bool:
        return self.role == "admin"


_DEV_USER = UserContext(
    broker_id="dev-broker-001",
    tenant_id="danube",
    role="broker",
    session_id="dev-session-001",
    display_name="Dev Broker",
    email="dev@danube.ae",
    org_id="danube-org",
)


# ── User store ────────────────────────────────────────────────────────────────

def _parse_users() -> dict[str, dict]:
    """
    Parse DANUBE_USERS env var → {username: {password, role}}.
    Format: "username:password:role,username2:password2"  (role defaults to "broker")
    """
    users: dict[str, dict] = {}
    for entry in settings.danube_users.split(","):
        parts = entry.strip().split(":")
        if len(parts) < 2:
            continue
        username = parts[0].strip()
        password = parts[1].strip()
        role     = parts[2].strip() if len(parts) > 2 else "broker"
        users[username] = {"password": password, "role": role}
    return users


def authenticate_user(username: str, password: str) -> dict | None:
    """Validate credentials. Returns user dict or None."""
    users = _parse_users()
    user  = users.get(username)
    if not user or user["password"] != password:
        return None
    return user


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_access_token(username: str, role: str) -> str:
    """Create a signed HS256 JWT valid for settings.token_ttl_seconds."""
    now     = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=settings.token_ttl_seconds)
    payload = {
        "sub":  username,
        "role": role,
        "sid":  uuid.uuid4().hex,
        "iat":  int(now.timestamp()),
        "exp":  int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_token(token: str) -> UserContext:
    """
    Decode and verify a backend-issued JWT.
    Raises AuthenticationError on expiry, tampered signature, or missing claims.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise AuthenticationError(f"Invalid or expired token: {exc}") from exc

    username = payload.get("sub")
    role     = payload.get("role", "broker")
    sid      = payload.get("sid", "")

    if not username:
        raise AuthenticationError("Token missing 'sub' claim.")

    return UserContext(
        broker_id=username,
        tenant_id="danube",
        role=role,
        session_id=sid,
        display_name=username,
        email=f"{username}@danube.ae",
        org_id="danube",
    )


# ── Dev user helper ───────────────────────────────────────────────────────────

def parse_dev_user_context(header_value: Optional[str]) -> UserContext:
    """Parse X-Dev-User-Context header. Falls back to _DEV_USER if absent/invalid."""
    if not header_value:
        return _DEV_USER
    try:
        data = json.loads(header_value)
        return UserContext(
            broker_id=data.get("broker_id", _DEV_USER.broker_id),
            tenant_id=data.get("tenant_id", _DEV_USER.tenant_id),
            role=data.get("role", _DEV_USER.role),
            session_id=data.get("session_id", _DEV_USER.session_id),
            display_name=data.get("display_name", _DEV_USER.display_name),
            email=data.get("email", _DEV_USER.email),
            org_id=data.get("org_id", _DEV_USER.org_id),
        )
    except (json.JSONDecodeError, TypeError):
        logger.warning("invalid_dev_user_context_header")
        return _DEV_USER


# ── RBAC ──────────────────────────────────────────────────────────────────────

class Role(str, Enum):
    broker = "broker"
    rm = "rm"
    admin = "admin"


ROLE_ASSET_PERMISSIONS: dict[str, list[str]] = {
    Role.broker: ["active"],
    Role.rm:     ["active", "draft"],
    Role.admin:  ["active", "draft", "archived"],
}

ADMIN_ROLES:     frozenset[str] = frozenset({Role.admin})
INGESTION_ROLES: frozenset[str] = frozenset({Role.admin, Role.rm})


# ── RBAC enforcement helpers ──────────────────────────────────────────────────

def allowed_asset_statuses(role: str) -> list[str]:
    """Return the asset statuses the given role may access."""
    return ROLE_ASSET_PERMISSIONS.get(role, [])


def require_admin(role: str) -> None:
    """Raise AuthorizationError if the role is not admin."""
    if role not in ADMIN_ROLES:
        raise AuthorizationError(f"Role '{role}' does not have admin access.")


def require_ingestion_permission(role: str) -> None:
    """Raise AuthorizationError if the role cannot ingest assets."""
    if role not in INGESTION_ROLES:
        raise AuthorizationError(f"Role '{role}' cannot ingest assets. Required: admin or rm.")
