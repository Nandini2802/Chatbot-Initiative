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
from urllib.parse import quote

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

    @staticmethod
    def _extract_list(
        data,
        endpoint: str,
    ) -> list[dict]:
        """
        Extract a list of records from a Salesforce response.

        Raise an error for unexpected response structures
        instead of treating them as zero results.
        """
        if isinstance(data, list):
            records = data

        elif isinstance(data, dict):
            records = None

            for key in (
                "ActiveProjects",
                "records",
                "data",
                "projects",
                "units",
                "Units",
            ):
                value = data.get(key)

                if isinstance(value, list):
                    records = value
                    break

            if records is None:
                raise SalesforceError(
                    f"Unexpected response structure from {endpoint}."
                )

        else:
            raise SalesforceError(
                f"Invalid response type from {endpoint}."
            )

        if not all(isinstance(item, dict) for item in records):
            raise SalesforceError(
                f"Invalid record structure from {endpoint}."
            )

        return records


    def __init__(self, user_id: str | None = None) -> None:
        self._user_id = user_id
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
                    params={
                        "grant_type": "client_credentials",
                        "client_id": settings.sf_client_id,
                        "client_secret": settings.sf_client_secret,
                    },
                    headers={
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                resp.raise_for_status()
            except httpx.TimeoutException as exc:
                raise SalesforceError("Salesforce token request timed out.") from exc
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code

                try:
                    response_body = exc.response.json()
                except ValueError:
                    response_body = {}

                error_code = (
                    response_body.get("error", "unknown")
                    if isinstance(response_body, dict)
                    else "unknown"
                )

                logger.error(
                    "salesforce_token_error: status=%s, error=%s",
                    status_code,
                    error_code,
                )

                raise SalesforceError(
                    f"Salesforce token request failed: "
                    f"HTTP {status_code}, error={error_code}"
                ) from exc

            body = resp.json()
            self._token = body["access_token"]
            # Salesforce tokens expire in ~2h; refresh 5 min early

            expires_in = body.get("expires_in")
            if expires_in is not None:
                lifetime = float(expires_in)
            else:
                lifetime = 7200.0

            self._token_expiry = (
                time.monotonic()
                + max(0.0, lifetime - 300.0)
            )

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

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }

        if self._user_id:
            headers["user_id"] = self._user_id

        try:
            resp = await self._http.get(
                path,
                headers=headers,
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

        try:
            return resp.json()
        except ValueError as exc:
            raise SalesforceError(
                f"Invalid JSON response from Salesforce: {path}"
            ) from exc


    # ── Public API calls ──────────────────────────────────────────────────


    async def get_active_projects(self) -> list[dict]:
        data = await self._get(
            "/services/apexrest/mobileApp/getActiveProjects"
        )

        projects = data.get("ActiveProjects", [])

        print("Total active projects:", len(projects))

        if projects and isinstance(projects[0], dict):
            print("First project field names:", list(projects[0].keys()))

        print("\n========== SALESFORCE ACTIVE PROJECTS ==========")
        print("Response type:", type(data).__name__)

        if isinstance(data, dict):
            print("Top-level keys:", list(data.keys()))

            for key, value in data.items():
                print(
                    f"Key: {key} | "
                    f"Type: {type(value).__name__}"
                )

                if isinstance(value, dict):
                    print(
                        "  Nested keys:",
                        list(value.keys())
                    )

        elif isinstance(data, list):
            print("Number of records:", len(data))

            if data and isinstance(data[0], dict):
                print(
                    "First record keys:",
                    list(data[0].keys())
                )

        print("===============================================\n")

        return self._extract_list(
            data,
            "getActiveProjects",
        )

    
    async def get_units_by_project(
        self,
        project_id: str,
    ) -> list[dict]:
        """
        Retrieve units for a verified Salesforce project ID.
        """
        if not project_id or not project_id.strip():
            raise SalesforceError(
                "A valid project ID is required."
            )

        safe_id = quote(
            project_id.strip(),
            safe="",
        )

        data = await self._get(
            f"{_APEX_BASE}/getUnitsByProject/{safe_id}"
        )

        return self._extract_list(
            data,
            "getUnitsByProject",
        )



    async def get_all_units(self) -> list[dict]:
        data = await self._get(
            f"{_APEX_BASE}/GetAllUnitsVisibleToBroker"
        )

        print("\n===== GET ALL UNITS RESPONSE =====")
        print("Response type:", type(data).__name__)

        if isinstance(data, dict):
            print("Response keys:", list(data.keys()))

            for key, value in data.items():
                print(f"{key}: {type(value).__name__}")

                if isinstance(value, list):
                    print(f"Number of records: {len(value)}")

        elif isinstance(data, list):
            print("Number of records:", len(data))

        else:
            print("Response:", repr(data)[:500])

        print("==================================\n")

        return self._extract_list(
            data,
            "GetAllUnitsVisibleToBroker",
        )
