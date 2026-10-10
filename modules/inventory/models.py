"""
Canonical Unit model and normalization functions.

Both Salesforce endpoints return inconsistent field names and structure.
This module normalises both into a single `Unit` dataclass so ALL
downstream logic (filtering, formatting) works on one shape only.

Endpoint differences:
  getUnitsByProject   → each record: {"unitId": ..., "fields": {...}}
  GetAllUnitsVisibleToBroker → flat record (no "fields" wrapper)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Any

import re

@dataclass(frozen=True)
class Project:
    project_id: str
    project_name: str
    building_name: Optional[str] = None
    location: Optional[str] = None
    construction_status: Optional[str] = None
    handover_status: Optional[str] = None
    starting_price: Optional[float] = None
    payment_plan: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Unit:
    unit_id: str
    unit_code: Optional[str]
    unit_name: Optional[str]
    project_id: Optional[str]
    project_name: Optional[str]
    building_name: Optional[str]
    category: Optional[str]          # Residential / Commercial / Hotel Apartment
    unit_type: Optional[str]
    typology: Optional[str]
    bedrooms: Optional[int]
    bathrooms: Optional[int]
    floor: Optional[int]
    area_sqft: Optional[float]
    price: Optional[float]
    status: str                      # "Available", "Booked", "Sold", …
    view: Optional[str]
    location: Optional[str]


def _clean_salesforce_keys(raw: dict) -> dict:
    """Remove accidental whitespace from Salesforce field names."""
    return {
        key.strip() if isinstance(key, str) else key: value
        for key, value in raw.items()
    }


def normalize_project(raw: dict) -> Project:
    """
    Normalize a project returned by getActiveProjects.

    Field mappings MUST be checked against the actual
    Salesforce getActiveProjects response.
    """

    raw = _clean_salesforce_keys(raw)

    fields = raw.get("fields")
    f = fields if isinstance(fields, dict) else raw

    def first(*keys):
        for key in keys:
            value = f.get(key)
            if value is not None and value != "":
                return value
        return None

    return Project(
        project_id=str(
            first("projectId", "ProjectId", "Id", "id") or ""
        ),
        project_name=str(
            first("projectName", "ProjectName", "Name") or ""
        ),
        building_name=first("BuildingName", "Building__c"),
        location=first("ProjectLocation", "Location__c", "Location_Name_Mob__c"),
        construction_status=first(
            "ConstructionStatus", "Construction_Status__c", "Construction_Status__c"
        ),
        handover_status=first(
            "HandoverStatus", "Handover_Status__c", "Anticipated_Handover_Date__c"
        ),
        starting_price=_to_float(
            first("StartingPrice", "Starting_Price__c", "Starting_Price_Mob__c")
        ),
        payment_plan=first("PaymentPlan", "Payment_Plan__c", "Payment_Plan_Mob__c"),
    )

def _to_floor(v) -> Optional[int]:
    if v is None:
        return None

    match = re.search(r"\d+", str(v))
    return int(match.group()) if match else None

def _to_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None

    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "")

        return float(value)
    except (ValueError, TypeError):
        return None


def normalize_from_project(raw: dict) -> Unit:
    """
    Normalise a record from getUnitsByProject/{projectId}.
    Each record has shape: {"unitId": str, "fields": {field_api_name: value, ...}}
    """
    f = raw.get("fields") or {}
    return Unit(
        unit_id=str(raw.get("unitId", "")),
        unit_code=f.get("Unit_Code__c"),
        unit_name=f.get("Name"),
        project_id=f.get("projectId"),
        project_name=f.get("projectName"),
        building_name=f.get("Building__c"),
        category=f.get("Category__c"),
        unit_type=f.get("Unit Type"),
        typology=f.get("Typology__c"),
        bedrooms=_to_int(f.get("NumberOfBedrooms__c")),
        bathrooms=_to_int(f.get("NumberOfBathrooms__c")),
        floor=_to_floor(f.get("FloorNumber__c")),
        area_sqft=_to_float(f.get("Total_Area_Sq_Ft__c")),
        price=_to_float(f.get("SellingPrice__c")),
        status=str(f.get("Status__c") or ""),
        view=f.get("View__c"),
        location=f.get("ProjectLocation"),
    )


def normalize_from_all(raw: dict) -> Unit:
    """
    Normalise a record from GetAllUnitsVisibleToBroker.
    Records are flat — no "fields" wrapper.
    """
    return Unit(
        unit_id=str(raw.get("unitId", "")),
        unit_code=raw.get("Unit_Code__c"),
        unit_name=raw.get("Name"),
        project_id=raw.get("projectId"),
        project_name=raw.get("projectName"),
        building_name=raw.get("BuildingName"),
        category=raw.get("Category__c"),
        unit_type=raw.get("Unit Type"),
        typology=raw.get("Typology__c"),
        bedrooms=_to_int(raw.get("NumberOfBedrooms")),
        bathrooms=_to_int(raw.get("NumberOfBathrooms")),
        floor=_to_floor(raw.get("FloorNumber__c")),
        area_sqft=_to_float(raw.get("Total_Saleable_Area_In_Sq_Ft__c")),
        price=_to_float(raw.get("SellingPrice__c")),
        status=str(raw.get("Status__c") or ""),
        view=raw.get("View__c"),
        location=raw.get("ProjectLocation"),
    )


def filter_available(units: list[Unit]) -> list[Unit]:
    """Keep only units where status == 'Available' (case-insensitive)."""
    return [u for u in units if u.status.strip().lower() == "available"]



def apply_filters(
    units: list[Unit],
    bedrooms: Optional[int] = None,
    category: Optional[str] = None,
    max_price: Optional[float] = None,
    min_price: Optional[float] = None,
    building_name: Optional[str] = None,
    unit_type: Optional[str] = None,
    unit_code: Optional[str] = None,
    floor: Optional[int] = None,
) -> list[Unit]:
    result = units

    if bedrooms is not None:
        result = [u for u in result if u.bedrooms == bedrooms]

    if category:
        result = [
            u for u in result
            if (u.category or "").casefold() == category.casefold()
        ]

    if max_price is not None:
        result = [
            u for u in result
            if u.price is not None and u.price <= max_price
        ]

    if min_price is not None:
        result = [
            u for u in result
            if u.price is not None and u.price >= min_price
        ]

    if building_name:
        result = [
            u for u in result
            if building_name.casefold()
            in (u.building_name or "").casefold()
        ]

    if unit_type:
        result = [
            u for u in result
            if unit_type.casefold()
            in (u.unit_type or "").casefold()
        ]

    if unit_code:
        result = [
            u for u in result
            if (u.unit_code or "").casefold() == unit_code.casefold()
            or u.unit_id.casefold() == unit_code.casefold()
        ]

    if floor is not None:
        result = [u for u in result if u.floor == floor]

    return result
