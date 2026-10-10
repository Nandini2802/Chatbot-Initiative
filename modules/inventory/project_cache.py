
"""
TTL-cached project directory for Salesforce inventory.

Maintains:
1. Active project records from getActiveProjects.
2. Unit project records from GetAllUnitsVisibleToBroker.

Project IDs from the two endpoints are kept separate.
"""

from __future__ import annotations

import asyncio
import logging
import time

from dataclasses import dataclass
from typing import Optional

from core.settings import settings
from modules.inventory.models import Project, normalize_project

logger = logging.getLogger(__name__)


@dataclass
class ProjectRecord:
    project_id: str
    project_name: str
    project: Optional[Project] = None
    unit_api_project_id: Optional[str] = None


def _normalize_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _extract_unit_projects(
    raw_units: list[dict],
) -> list[ProjectRecord]:
    seen: dict[str, ProjectRecord] = {}

    for raw in raw_units:
        project_id = raw.get("projectId") or raw.get("Project__c")
        project_name = (
            raw.get("projectName") or raw.get("Project_Name__c")
        )

        if not project_id or not project_name:
            continue

        key = _normalize_name(str(project_name))

        if key not in seen:
            seen[key] = ProjectRecord(
                project_id=str(project_id),
                project_name=str(project_name),
            )

    return list(seen.values())


def _extract_active_projects(
    raw_projects: list[dict],
) -> list[ProjectRecord]:
    records = []

    for raw in raw_projects:
        project = normalize_project(raw)

        if not project.project_name:
            continue

        records.append(
            ProjectRecord(
                project_id=project.project_id,
                project_name=project.project_name,
                project=project,
            )
        )

    return records


class ProjectCache:
    def __init__(
        self,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else settings.sf_project_cache_ttl_seconds
        )

        self._active_projects: list[ProjectRecord] = []
        self._unit_projects: list[ProjectRecord] = []

        self._active_fetched_at = 0.0
        self._units_fetched_at = 0.0

        self._active_loaded = False
        self._units_loaded = False

        self._lock = asyncio.Lock()

    def _is_fresh(self, fetched_at: float, loaded: bool) -> bool:
        return (
            loaded
            and time.monotonic() - fetched_at < self._ttl
        )

    async def get_active_projects(self) -> list[ProjectRecord]:
        if self._is_fresh(
            self._active_fetched_at, self._active_loaded
        ):
            return list(self._active_projects)

        async with self._lock:
            if self._is_fresh(
                self._active_fetched_at, self._active_loaded
            ):
                return list(self._active_projects)

            from modules.inventory.client import AsyncSalesforceClient

            async with AsyncSalesforceClient() as client:
                raw = await client.get_active_projects()

            records = _extract_active_projects(raw)

            self._active_projects = records
            self._active_fetched_at = time.monotonic()
            self._active_loaded = True

            logger.info(
                "sf_active_project_cache_refreshed",
                extra={"count": len(records)},
            )

            return list(records)

    async def get_unit_projects(self) -> list[ProjectRecord]:
        if self._is_fresh(
            self._units_fetched_at, self._units_loaded
        ):
            return list(self._unit_projects)

        async with self._lock:
            if self._is_fresh(
                self._units_fetched_at, self._units_loaded
            ):
                return list(self._unit_projects)

            from modules.inventory.client import AsyncSalesforceClient

            async with AsyncSalesforceClient() as client:
                raw = await client.get_all_units()

            records = _extract_unit_projects(raw)

            self._unit_projects = records
            self._units_fetched_at = time.monotonic()
            self._units_loaded = True

            logger.info(
                "sf_unit_project_cache_refreshed",
                extra={"count": len(records)},
            )

            return list(records)

    async def get_all(self) -> list[ProjectRecord]:
        """
        Backward-compatible directory for name resolution.

        Active projects are primary. Unit-only projects are
        included so existing inventory queries still work.
        """
        active = await self.get_active_projects()
        unit_projects = await self.get_unit_projects()

        merged: dict[str, ProjectRecord] = {}

        for record in active:
            merged[_normalize_name(record.project_name)] = record

        for record in unit_projects:
            key = _normalize_name(record.project_name)

            if key not in merged:
                merged[key] = record

        return list(merged.values())

    def invalidate(self) -> None:
        self._active_fetched_at = 0.0
        self._units_fetched_at = 0.0
        self._active_loaded = False
        self._units_loaded = False


_cache: Optional[ProjectCache] = None


def get_project_cache() -> ProjectCache:
    global _cache

    if _cache is None:
        _cache = ProjectCache()

    return _cache
