"""
Card Pydantic models — one model per card type.

Variable names must match the Jinja2 template variable names exactly.
Templates live in templates/cards/*.html.j2
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class BrochureCard(BaseModel):
    project: str
    language: str
    version_label: Optional[str] = None
    download_url: str          # signed URL — never a raw blob path
    file_size_bytes: Optional[int] = None
    content_type: str = "application/pdf"


class FloorPlanImage(BaseModel):
    url: str
    file_name: str
    order: int = 0


class FloorPlanCard(BaseModel):
    project: str
    language: str
    preview_images: list[FloorPlanImage]   # first 6 only
    total_images: int
    zip_url: Optional[str] = None          # live folder ZIP URL


class GalleryImage(BaseModel):
    url: str
    caption: Optional[str] = None
    order: int = 0


class GalleryCard(BaseModel):
    project: str
    preview_images: list[GalleryImage]     # first 6 only
    total_images: int
    folder_label: str = "Gallery"          # e.g. "Interiors", "Exteriors"
    zip_url: Optional[str] = None          # live folder ZIP URL


class VideoCard(BaseModel):
    project: str
    stream_url: str            # signed URL or streaming URL
    thumbnail_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    language: str = "en"


class VersionEntry(BaseModel):
    version_label: str
    is_current: bool
    released_at: Optional[str] = None
    release_notes: Optional[str] = None


class VersionCheckCard(BaseModel):
    project: str
    asset_type: str
    language: str
    current_version: str
    all_versions: list[VersionEntry]
    is_latest: bool            # True when broker's material matches current_version


class LanguageEntry(BaseModel):
    language_code: str         # e.g. "en", "ar", "ru"
    language_name: str         # e.g. "English", "Arabic", "Russian"
    is_available: bool = True


class LanguageListCard(BaseModel):
    project: str
    asset_type: str
    languages: list[LanguageEntry]


class ProjectOverviewCard(BaseModel):
    project: str
    location: Optional[str] = None
    description: Optional[str] = None
    available_asset_types: list[str]   # ["brochure", "floor_plan", "gallery", "video"]
    available_languages: list[str]
    thumbnail_url: Optional[str] = None
