
"""
Salesforce inventory orchestration service.

Supported intents:
- inventory_project
- inventory_units
- inventory_combined
- inventory_availability (legacy)

Project data comes from getActiveProjects.
Unit data comes from GetAllUnitsVisibleToBroker.

The project-specific units API can be enabled after
its project ID contract has been verified.
"""

from __future__ import annotations

import logging

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.errors import SalesforceError

from modules.inventory.client import AsyncSalesforceClient
from modules.inventory.models import (
    Project,
    Unit,
    apply_filters,
    filter_available,
    normalize_from_all,
)
from modules.inventory.project_cache import get_project_cache
from modules.inventory.resolver import (
    ResolveStatus,
    resolve_project_name,
)

logger = logging.getLogger(__name__)


class ResultKind(str, Enum):
    PROJECT = "project"
    UNITS = "units"
    COMBINED = "combined"

    AMBIGUOUS_PROJECT = "ambiguous_project"
    PROJECT_NOT_FOUND = "project_not_found"
    SF_ERROR = "sf_error"


@dataclass
class InventoryResult:
    kind: ResultKind

    project: Optional[Project] = None
    units: list[Unit] = field(default_factory=list)

    project_name: Optional[str] = None
    project_id: Optional[str] = None

    is_global: bool = False

    candidates: list[str] = field(default_factory=list)
    raw_query_project: Optional[str] = None

    filters_applied: dict = field(default_factory=dict)
    error_message: Optional[str] = None


def _to_int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _to_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _extract_filters(entities: dict) -> dict:
    filters = {
        "bedrooms": _to_int(entities.get("bedrooms")),
        "category": entities.get("category"),
        "min_price": _to_float(entities.get("min_price")),
        "max_price": _to_float(entities.get("max_price")),
        "building_name": entities.get("building_name"),
        "unit_type": entities.get("unit_type"),
        "unit_code": entities.get("unit_code"),
        "floor": _to_int(entities.get("floor")),
    }

    return {
        key: value
        for key, value in filters.items()
        if value is not None and value != ""
    }


async def _resolve(
    project_name: str,
    source: str,
) -> tuple[Optional[InventoryResult], object]:
    """
    Resolve project name.

    Returns:
    - error result, None
    - None, resolved record
    """
    resolution = await resolve_project_name(
        project_name,
        source=source,
    )

    if resolution.status == ResolveStatus.NOT_FOUND:
        return (
            InventoryResult(
                kind=ResultKind.PROJECT_NOT_FOUND,
                raw_query_project=project_name,
            ),
            None,
        )

    if resolution.status == ResolveStatus.AMBIGUOUS:
        return (
            InventoryResult(
                kind=ResultKind.AMBIGUOUS_PROJECT,
                raw_query_project=project_name,
                candidates=[
                    record.project_name
                    for record in resolution.candidates
                ],
            ),
            None,
        )

    return None, resolution


async def _get_project_details(
    project_name: str,
) -> InventoryResult:
    """
    Project-level Salesforce lookup.
    """

    error, resolved = await _resolve(
        project_name,
        source="active",
    )


    if error is not None:
        return error


    cache = get_project_cache()
    projects = await cache.get_active_projects()

    record = next(
        (
            record
            for record in projects
            if record.project_id == resolved.project_id
            and record.project_name == resolved.project_name
        ),
        None,
    )


    if record is None or record.project is None:
        return InventoryResult(
            kind=ResultKind.PROJECT_NOT_FOUND,
            raw_query_project=project_name,
        )

        

    return InventoryResult(
        kind=ResultKind.PROJECT,
        project=record.project,
        project_name=record.project_name,
        project_id=record.project_id,
    )


async def _get_unit_inventory(
    entities: dict,
) -> InventoryResult:
    """
    Unit-level Salesforce lookup.

    Uses the global visible-units endpoint and filters
    by project locally, preserving existing behavior.
    """
    project_name = entities.get("project_name")
    filters = _extract_filters(entities)

    resolved = None

    if project_name:
        error, resolved = await _resolve(
            project_name,
            source="units",
        )

        if error is not None:
            return error

    async with AsyncSalesforceClient() as client:
        raw_units = await client.get_all_units()

    all_units = [
        normalize_from_all(raw)
        for raw in raw_units
    ]

    if resolved is not None:
        resolved_name = resolved.project_name.casefold()

        all_units = [
            unit
            for unit in all_units
            if (unit.project_name or "").casefold()
            == resolved_name
        ]

    available = filter_available(all_units)

    filtered = apply_filters(
        available,
        **filters,
    )

    logger.info(
        "inventory_units_result",
        extra={
            "project": (
                resolved.project_name
                if resolved is not None
                else None
            ),
            "available": len(available),
            "after_filters": len(filtered),
        },
    )

    return InventoryResult(
        kind=ResultKind.UNITS,
        units=filtered,
        project_name=(
            resolved.project_name
            if resolved is not None
            else None
        ),
        project_id=(
            resolved.project_id
            if resolved is not None
            else None
        ),
        is_global=resolved is None,
        filters_applied=filters,
    )


async def _get_combined_inventory(
    entities: dict,
) -> InventoryResult:
    """
    Retrieve project-level and unit-level data.

    Both datasets must be available for a successful
    combined result.
    """
    project_name = entities.get("project_name")

    if not project_name:
        return InventoryResult(
            kind=ResultKind.PROJECT_NOT_FOUND,
            raw_query_project="",
        )

    project_result = await _get_project_details(
        project_name
    )

    if project_result.kind != ResultKind.PROJECT:
        return project_result

    # Use the canonical project name returned
    # by the project directory.
    unit_entities = dict(entities)
    unit_entities["project_name"] = (
        project_result.project_name
    )

    unit_result = await _get_unit_inventory(
        unit_entities
    )

    if unit_result.kind == ResultKind.AMBIGUOUS_PROJECT:
        return unit_result

    if unit_result.kind == ResultKind.PROJECT_NOT_FOUND:
        # A project can exist in getActiveProjects
        # but have no visible unit records.
        return InventoryResult(
            kind=ResultKind.COMBINED,
            project=project_result.project,
            project_name=project_result.project_name,
            project_id=project_result.project_id,
            units=[],
            is_global=False,
            filters_applied=_extract_filters(entities),
        )

    if unit_result.kind != ResultKind.UNITS:
        return unit_result

    return InventoryResult(
        kind=ResultKind.COMBINED,
        project=project_result.project,
        project_name=project_result.project_name,
        project_id=project_result.project_id,
        units=unit_result.units,
        is_global=False,
        filters_applied=unit_result.filters_applied,
    )


async def answer_inventory_query(
    entities: dict,
    inventory_intent: str = "inventory_units",
) -> InventoryResult:
    """
    Main entry point for the inventory graph node.

    Selects the appropriate Salesforce data retrieval
    workflow based on the classified intent.
    """
    entities = entities or {}

    project_name = entities.get("project_name")

    try:
        if inventory_intent == "inventory_project":
            if not project_name:
                return InventoryResult(
                    kind=ResultKind.PROJECT_NOT_FOUND,
                    raw_query_project="",
                )

            return await _get_project_details(
                project_name
            )

        if inventory_intent in {
            "inventory_units",
            "inventory_availability",
        }:
            return await _get_unit_inventory(
                entities
            )

        if inventory_intent == "inventory_combined":
            return await _get_combined_inventory(
                entities
            )

        logger.warning(
            "unknown_inventory_intent",
            extra={"intent": inventory_intent},
        )

        return InventoryResult(
            kind=ResultKind.SF_ERROR,
            error_message=(
                f"Unsupported inventory intent: "
                f"{inventory_intent}"
            ),
        )

    except SalesforceError as exc:
        logger.exception(
            "inventory_salesforce_error: intent=%s, error=%s",
            inventory_intent,
            str(exc),
        )

        return InventoryResult(
            kind=ResultKind.SF_ERROR,
            error_message=str(exc),
            is_global=not bool(project_name),
        )

