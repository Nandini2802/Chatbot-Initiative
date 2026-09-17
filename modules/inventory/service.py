"""
Inventory service — orchestrates the full availability query pipeline.

Flow:
  1. Fetch ALL units from GetAllUnitsVisibleToBroker (single live call).
  2. If project_name given → resolve via fuzzy resolver (uses project cache
     which is also built from the same endpoint, cached for TTL).
     - RESOLVED  → filter fetched units by canonical project name
     - AMBIGUOUS → return clarification payload
     - NOT_FOUND → return not-found payload
  3. No project_name → use all fetched units (global).
  4. filter_available (status == "Available")
  5. apply_filters (bedrooms, category, max_price)
  6. Return InventoryResult — consumed by inventory_query_node.

One API call per inventory query regardless of whether a project is named.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.errors import SalesforceError  # used in answer_inventory_query top-level fetch
from modules.inventory.client import AsyncSalesforceClient
from modules.inventory.models import Unit, apply_filters, filter_available
from modules.inventory.models import normalize_from_all
from modules.inventory.project_cache import get_project_cache
from modules.inventory.resolver import ResolveStatus, resolve_project_name

logger = logging.getLogger(__name__)


class ResultKind(str, Enum):
    UNITS = "units"               # normal result — units list may be empty
    AMBIGUOUS_PROJECT = "ambiguous_project"
    PROJECT_NOT_FOUND = "project_not_found"
    SF_ERROR = "sf_error"


@dataclass
class InventoryResult:
    kind: ResultKind
    units: list[Unit] = field(default_factory=list)
    project_name: Optional[str] = None   # resolved name (UNITS path)
    project_id: Optional[str] = None
    is_global: bool = False               # True when no project was named
    candidates: list[str] = field(default_factory=list)  # AMBIGUOUS
    raw_query_project: Optional[str] = None  # what the user typed
    filters_applied: dict = field(default_factory=dict)
    error_message: Optional[str] = None


async def answer_inventory_query(entities: dict) -> InventoryResult:
    """
    Main entry point called by inventory_query_node.

    Fetches all units in a single GetAllUnitsVisibleToBroker call, then
    branches on whether a project name was given.
    """
    project_name_raw: Optional[str] = entities.get("project_name")
    bedrooms: Optional[int] = _to_int(entities.get("bedrooms"))
    category: Optional[str] = entities.get("category")
    max_price: Optional[float] = _to_float(entities.get("max_price"))

    filters_applied = {
        k: v for k, v in {
            "bedrooms": bedrooms,
            "category": category,
            "max_price": max_price,
        }.items() if v is not None
    }

    # ── Single live fetch ─────────────────────────────────────────────────
    try:
        async with AsyncSalesforceClient() as client:
            raw_units = await client.get_all_units()
    except SalesforceError as exc:
        logger.error("inventory_sf_error_global", extra={"error": str(exc)})
        return InventoryResult(
            kind=ResultKind.SF_ERROR,
            error_message=str(exc),
            is_global=not bool(project_name_raw),
        )

    all_units = [normalize_from_all(r) for r in raw_units]

    # ── Branch: project named vs. global ──────────────────────────────────
    if project_name_raw:
        return await _project_scoped_query(
            project_name_raw, all_units, bedrooms, category, max_price, filters_applied
        )
    return _global_query(all_units, bedrooms, category, max_price, filters_applied)


async def _project_scoped_query(
    project_name_raw: str,
    all_units: list[Unit],
    bedrooms: Optional[int],
    category: Optional[str],
    max_price: Optional[float],
    filters_applied: dict,
) -> InventoryResult:
    # Resolve the project name against the cache (built from same endpoint, TTL-cached)
    cache = get_project_cache()
    resolve = await resolve_project_name(project_name_raw, cache)

    if resolve.status == ResolveStatus.AMBIGUOUS:
        return InventoryResult(
            kind=ResultKind.AMBIGUOUS_PROJECT,
            raw_query_project=project_name_raw,
            candidates=[p.project_name for p in resolve.candidates],
        )

    if resolve.status == ResolveStatus.NOT_FOUND:
        return InventoryResult(
            kind=ResultKind.PROJECT_NOT_FOUND,
            raw_query_project=project_name_raw,
        )

    resolved_name = resolve.project_name
    resolved_name_lower = resolved_name.lower()

    # Filter the already-fetched units by resolved project name
    project_units = [
        u for u in all_units
        if (u.project_name or "").lower() == resolved_name_lower
    ]
    available = filter_available(project_units)
    filtered = apply_filters(available, bedrooms=bedrooms, category=category, max_price=max_price)

    logger.info(
        "inventory_project_result",
        extra={
            "project": resolved_name,
            "total_in_project": len(project_units),
            "available": len(available),
            "after_filters": len(filtered),
            "filters": filters_applied,
        },
    )

    return InventoryResult(
        kind=ResultKind.UNITS,
        units=filtered,
        project_name=resolved_name,
        project_id=resolve.project_id,
        is_global=False,
        filters_applied=filters_applied,
    )


def _global_query(
    all_units: list[Unit],
    bedrooms: Optional[int],
    category: Optional[str],
    max_price: Optional[float],
    filters_applied: dict,
) -> InventoryResult:
    available = filter_available(all_units)
    filtered = apply_filters(available, bedrooms=bedrooms, category=category, max_price=max_price)

    logger.info(
        "inventory_global_result",
        extra={
            "total": len(all_units),
            "available": len(available),
            "after_filters": len(filtered),
            "filters": filters_applied,
        },
    )

    return InventoryResult(
        kind=ResultKind.UNITS,
        units=filtered,
        is_global=True,
        filters_applied=filters_applied,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None
