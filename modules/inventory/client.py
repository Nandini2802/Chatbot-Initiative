"""
Salesforce HTTP client for inventory endpoints.

Responsibilities:
- OAuth2 client-credentials token acquisition and in-memory caching.
- All GET calls to the Salesforce Apex REST endpoints.
- Logging the Sforce-Limit-Info header on every response.
- Warning when remaining daily API quota drops below the configured threshold.
- Raising SalesforceError (never raw httpx exceptions) so callers handle one type.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from core.errors import InventoryUnavailableError, SalesforceError
from core.settings import settings

logger = logging.getLogger(__name__)

_APEX_BASE = "/services/apexrest/mobileApp"
_TOKEN_PATH = "/services/oauth2/token"

# Daily API request limit for this Salesforce org edition.
# Used only for threshold % calculation when the header doesn't include the max.
# Salesforce Enterprise = 15 000 × number_of_licenses; adjust here once confirmed.
_ASSUMED_DAILY_LIMIT = 15_000


class AsyncSalesforceClient:
    """
    Thin async wrapper around the Salesforce Apex REST API.

    Usage:
        async with AsyncSalesforceClient() as client:
            data = await client.get_active_projects()
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "AsyncSalesforceClient":
        self._http = httpx.AsyncClient(
            base_url=settings.sf_instance_url,
            timeout=settings.sf_api_timeout_seconds,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._http:
            await self._http.aclose()

    # ── Token management ──────────────────────────────────────────────────

    async def _get_token(self) -> str:
        """Return a valid access token, refreshing if expired."""
        async with self._lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token

            if not settings.sf_client_id or not settings.sf_client_secret:
                raise SalesforceError(
                    "Salesforce credentials not configured. "
                    "Set SF_CLIENT_ID and SF_CLIENT_SECRET in .env."
                )

            assert self._http is not None
            try:
                resp = await self._http.post(
                    _TOKEN_PATH,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": settings.sf_client_id,
                        "client_secret": settings.sf_client_secret,
                    },
                )
                resp.raise_for_status()
            except httpx.TimeoutException as exc:
                raise SalesforceError("Salesforce token request timed out.") from exc
            except httpx.HTTPStatusError as exc:
                raise SalesforceError(
                    f"Salesforce token request failed: HTTP {exc.response.status_code}"
                ) from exc

            body = resp.json()
            self._token = body["access_token"]
            # Salesforce tokens expire in ~2h; refresh 5 min early
            issued_at = int(body.get("issued_at", 0)) / 1000  # ms → s
            self._token_expiry = (issued_at or time.monotonic()) + 7200 - 300
            logger.info("sf_token_refreshed")
            return self._token

    # ── Quota logging ─────────────────────────────────────────────────────

    def _log_quota(self, headers: httpx.Headers, endpoint: str) -> None:
        """Parse and log Sforce-Limit-Info; warn if quota is low."""
        limit_info = headers.get("Sforce-Limit-Info", "")
        # Example value: "api-usage=1234; api-usage-last-24-hours=5678"
        logger.info(
            "sf_api_quota",
            extra={"endpoint": endpoint, "sforce_limit_info": limit_info},
        )
        # Parse remaining from "api-usage=<used>"
        try:
            for part in limit_info.split(";"):
                part = part.strip()
                if part.startswith("api-usage="):
                    used = int(part.split("=")[1].strip())
                    remaining = _ASSUMED_DAILY_LIMIT - used
                    threshold = int(_ASSUMED_DAILY_LIMIT * settings.sf_quota_warn_threshold)
                    if remaining <= threshold:
                        logger.warning(
                            "sf_quota_low",
                            extra={
                                "remaining": remaining,
                                "threshold": threshold,
                                "endpoint": endpoint,
                            },
                        )
                        if remaining <= 0:
                            raise InventoryUnavailableError(
                                "Salesforce API daily quota exhausted. "
                                "Inventory lookup is temporarily unavailable."
                            )
        except InventoryUnavailableError:
            raise
        except Exception:
            pass  # malformed header — don't crash

    # ── GET helper ────────────────────────────────────────────────────────

    async def _get(self, path: str) -> Any:
        assert self._http is not None
        token = await self._get_token()
        try:
            resp = await self._http.get(
                path,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise SalesforceError(
                f"Salesforce request timed out: {path}"
            ) from exc
        except httpx.RequestError as exc:
            raise SalesforceError(
                f"Salesforce request failed: {exc}"
            ) from exc

        self._log_quota(resp.headers, path)

        if resp.status_code == 401:
            # Token may have been invalidated externally — force refresh next call
            async with self._lock:
                self._token = None
                self._token_expiry = 0.0
            raise SalesforceError("Salesforce authentication failed. Token will be refreshed on next request.")

        if not resp.is_success:
            raise SalesforceError(
                f"Salesforce returned HTTP {resp.status_code} for {path}"
            )

        return resp.json()

    # ── Public API calls ──────────────────────────────────────────────────

    async def get_active_projects(self) -> list[dict]:
        """GET getActiveProjects — returns the full project directory."""
        data = await self._get(f"{_APEX_BASE}/getActiveProjects")
        if isinstance(data, list):
            return data
        # Some Apex REST responses wrap the list in a root key
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    async def get_units_by_project(self, project_id: str) -> list[dict]:
        """GET getUnitsByProject/{projectId} — unit-level records for one project."""
        data = await self._get(f"{_APEX_BASE}/getUnitsByProject/{project_id}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []

    async def get_all_units(self) -> list[dict]:
        """GET GetAllUnitsVisibleToBroker — flat unit records across all projects."""
        data = await self._get(f"{_APEX_BASE}/GetAllUnitsVisibleToBroker")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
        return []
