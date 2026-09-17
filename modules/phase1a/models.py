"""
Phase 1A data models — plain Python dataclasses (no ORM, no DB).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Project:
    id: str
    tenant_id: str
    name: str
    name_normalized: str
    description: Optional[str] = None
    location: Optional[str] = None
    is_active: bool = True


@dataclass
class Asset:
    id: str
    tenant_id: str
    project_id: str
    asset_type: str
    language: str
    blob_path: str
    content_type: str
    status: str
    permissions: list[str] = field(default_factory=list)
    unit_type: Optional[str] = None
    file_size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None


@dataclass
class AssetVersion:
    id: str
    tenant_id: str
    asset_id: str
    version_label: str
    blob_path: str
    is_current: bool = True
    release_notes: Optional[str] = None
    released_by: Optional[str] = None


@dataclass
class ShareLink:
    id: str
    tenant_id: str
    slug: str
    asset_id: str
    created_by: Optional[str] = None
    expires_at: Optional[datetime] = None
    is_active: bool = True
