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

from dataclasses import dataclass
from typing import Optional


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
        floor=_to_int(f.get("FloorNumber__c")),
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
        floor=_to_int(raw.get("FloorNumber__c")),
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
) -> list[Unit]:
    """Apply optional entity filters on top of an already-available unit list."""
    result = units
    if bedrooms is not None:
        result = [u for u in result if u.bedrooms == bedrooms]
    if category:
        cat_lower = category.strip().lower()
        result = [u for u in result if (u.category or "").lower() == cat_lower]
    if max_price is not None:
        result = [u for u in result if u.price is not None and u.price <= max_price]
    return result
