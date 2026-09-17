"""
In-memory project directory cache with TTL.

Builds the project directory from GetAllUnitsVisibleToBroker by extracting
unique projectId/projectName pairs. This guarantees the projectIds and
projectNames here are the same format as what appears in unit records —
avoiding the mismatch that occurs when using getActiveProjects IDs with
getUnitsByProject.

Only this module calls GetAllUnitsVisibleToBroker for directory purposes.
All other code goes through get_project_cache().
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from core.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ProjectRecord:
    """Minimal project shape needed for name resolution."""
    project_id: str       # projectId as it appears in unit records
    project_name: str     # projectName as it appears in unit records


def _extract_projects(raw_units: list[dict]) -> list[ProjectRecord]:
    """
    Derive unique ProjectRecord entries from a flat all-units response.
    Uses (projectId, projectName) from each unit record, deduped by project_id.
    """
    seen: dict[str, ProjectRecord] = {}
    for u in raw_units:
        pid = u.get("projectId") or u.get("Project__c")
        name = u.get("projectName") or u.get("Project_Name__c")
        if pid and name and str(pid) not in seen:
            seen[str(pid)] = ProjectRecord(
                project_id=str(pid),
                project_name=str(name),
            )
    return list(seen.values())


class ProjectCache:
    """
    Lazy, TTL-based in-memory cache for the project directory.

    Single instance used as a module-level singleton (see get_project_cache()).
    """

    def __init__(self, ttl_seconds: Optional[int] = None) -> None:
        self._ttl = ttl_seconds or settings.sf_project_cache_ttl_seconds
        self._projects: list[ProjectRecord] = []
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_fresh(self) -> bool:
        return bool(self._projects) and (time.monotonic() - self._fetched_at) < self._ttl

    async def get_all(self) -> list[ProjectRecord]:
        """Return cached project list, refreshing if TTL has expired."""
        if self._is_fresh():
            return self._projects

        async with self._lock:
            # Double-check inside lock to avoid thundering herd on expiry
            if self._is_fresh():
                return self._projects

            from modules.inventory.client import AsyncSalesforceClient

            logger.info("sf_project_cache_refresh")
            async with AsyncSalesforceClient() as client:
                raw_units = await client.get_all_units()

            projects = _extract_projects(raw_units)

            self._projects = projects
            self._fetched_at = time.monotonic()
            logger.info(
                "sf_project_cache_refreshed",
                extra={"count": len(projects)},
            )
            return self._projects

    def invalidate(self) -> None:
        """Force a refresh on the next call (useful for tests)."""
        self._fetched_at = 0.0


# Module-level singleton — one cache per process lifetime
_cache: Optional[ProjectCache] = None


def get_project_cache() -> ProjectCache:
    global _cache
    if _cache is None:
        _cache = ProjectCache()
    return _cache
