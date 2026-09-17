"""
Asset registry — backed by Postgres (marketing_collateral_assets table).

Data is populated by: scripts/sync_marketing_collateral.py
Run the sync before starting the app, and on a nightly schedule.
"""
from __future__ import annotations

import requests
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, func

from core.auth import UserContext
from core.db.session import AsyncSessionLocal
from core.db.models import MarketingCollateralAsset, MarketingCollateralProject
from core.errors import NotFoundError
from core.observability import tracer

logger = logging.getLogger(__name__)


# ── asset_type → folder_keys mapping ─────────────────────────────────────────
# Maps chatbot intent asset types to the folder_key values in the DB.
# Listed in priority order — first match with results wins.
_ASSET_TYPE_FOLDERS: dict[str, list[str]] = {
    "brochure":         ["brochures"],
    "floor_plan":       ["floor-plans"],
    "gallery":          ["interiors", "exteriors", "amenities", "renders"],
    "video":            ["videos", "walkthrough", "social-media"],
    "project_overview": ["other", "factsheet"],
}

# mime_type → content_type header
_MIME_MAP: dict[str, str] = {
    "application/pdf": "application/pdf",
    "image/jpeg":      "image/jpeg",
    "image/png":       "image/png",
    "image/webp":      "image/webp",
    "video/mp4":       "video/mp4",
}

# ── Middleware API config (for live ZIP URLs) ─────────────────────────────────
_MC_API_URL = "https://middleware.danubeproperties.com/api/marketing_collateral"
_MC_API_HEADERS = {
    "x_api_key": "hffghfgVb$)7yV;e0N<{e1t@,_S{_LNovryojjUpJTO#-sD4gE*HHcpBX(Q%KEK6ATy6lQK",
    "Content-Type": "application/json",
}


# ── Data shapes (unchanged — card_builder depends on these) ───────────────────

@dataclass
class AssetRow:
    id: str
    project_id: str
    project_name: str
    asset_type: str
    language: str
    unit_type: Optional[str]
    blob_path: str
    content_type: str
    status: str
    version_label: Optional[str]
    is_current_version: bool
    tenant_id: str


@dataclass
class VersionRow:
    asset_id: str
    project_name: str
    version_label: str
    is_current: bool
    released_at: Optional[str]
    release_notes: Optional[str]
    asset_type: str
    language: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalise(name: str) -> str:
    return name.lower().strip()


def _lang_sql_filter(language: str):
    """Return a SQLAlchemy filter that matches files for the given language."""
    from sqlalchemy import or_
    col = MarketingCollateralAsset.file_name
    if language == "ar":
        return func.lower(col).contains("_ar.")
    if language == "ru":
        return func.lower(col).contains("_ru.")
    if language == "zh":
        return or_(func.lower(col).contains("chinese"), func.lower(col).contains("_zh."))
    # Default: English — exclude known non-English markers
    return ~or_(
        func.lower(col).contains("_ar."),
        func.lower(col).contains("_ru."),
        func.lower(col).contains("chinese"),
        func.lower(col).contains("_zh."),
    )


def _row_to_asset(row: MarketingCollateralAsset, asset_type: str, language: str, tenant_id: str, unit_type: Optional[str]) -> AssetRow:
    content_type = _MIME_MAP.get(row.mime_type or "", "application/octet-stream")
    return AssetRow(
        id=str(row.id),
        project_id=str(row.project_id),
        project_name=row.project_title,
        asset_type=asset_type,
        language=language,
        unit_type=unit_type,
        blob_path=row.url,
        content_type=content_type,
        status="active",
        version_label=row.uploaded_at.strftime("v%Y%m%d") if row.uploaded_at else "v1",
        is_current_version=True,
        tenant_id=tenant_id,
    )


# ── Registry ──────────────────────────────────────────────────────────────────

class AssetRegistry:

    async def get_asset(
        self,
        project_name: str,
        asset_type: str,
        user: UserContext,
        language: str = "en",
        unit_type: Optional[str] = None,
    ) -> AssetRow:
        with tracer.start_as_current_span("asset_registry.get_asset"):
            folder_keys = _ASSET_TYPE_FOLDERS.get(asset_type)
            if not folder_keys:
                raise NotFoundError(f"Unknown asset type '{asset_type}'.")

            normalised = _normalise(project_name)
            async with AsyncSessionLocal() as session:
                for folder_key in folder_keys:
                    # Try language-matched file first (e.g. _ar, _ru), then fall back to English/default
                    lang_filter = _lang_sql_filter(language)
                    for fname_filter in (lang_filter, None):
                        stmt = (
                            select(MarketingCollateralAsset)
                            .where(
                                func.lower(MarketingCollateralAsset.project_title) == normalised,
                                MarketingCollateralAsset.folder_key == folder_key,
                                MarketingCollateralAsset.is_active.is_(True),
                                *([] if fname_filter is None else [fname_filter]),
                            )
                            .order_by(MarketingCollateralAsset.uploaded_at.asc())
                            .limit(1)
                        )
                        result = await session.execute(stmt)
                        row = result.scalar_one_or_none()
                        if row:
                            return _row_to_asset(row, asset_type, language, user.tenant_id, unit_type)

            raise NotFoundError(
                f"No {asset_type} found for project '{project_name}'. "
                "Ensure the sync script has been run: python scripts/sync_marketing_collateral.py"
            )

    async def get_all_assets(
        self,
        project_name: str,
        asset_type: str,
        user: UserContext,
        language: str = "en",
        folder_key: Optional[str] = None,
    ) -> list[AssetRow]:
        """Return all active assets for a project+asset_type (e.g. all floor plan images).
        Pass folder_key to restrict to a specific subfolder (e.g. 'interiors')."""
        with tracer.start_as_current_span("asset_registry.get_all_assets"):
            if folder_key:
                folder_keys = [folder_key]
            else:
                folder_keys = _ASSET_TYPE_FOLDERS.get(asset_type)
                if not folder_keys:
                    raise NotFoundError(f"Unknown asset type '{asset_type}'.")

            normalised = _normalise(project_name)
            rows: list[AssetRow] = []

            async with AsyncSessionLocal() as session:
                for fk in folder_keys:
                    stmt = (
                        select(MarketingCollateralAsset)
                        .where(
                            func.lower(MarketingCollateralAsset.project_title) == normalised,
                            MarketingCollateralAsset.folder_key == fk,
                            MarketingCollateralAsset.is_active.is_(True),
                        )
                        .order_by(MarketingCollateralAsset.file_name)
                    )
                    result = await session.execute(stmt)
                    for db_row in result.scalars():
                        rows.append(_row_to_asset(db_row, asset_type, language, user.tenant_id, None))
                    if rows:
                        break  # use first folder_key that has results

            if not rows:
                raise NotFoundError(
                    f"No {asset_type} found for project '{project_name}'. "
                    "Ensure the sync script has been run: python scripts/sync_marketing_collateral.py"
                )
            return rows

    async def get_folder_zip_url(
        self,
        project_name: str,
        folder_key: str,
    ) -> Optional[str]:
        """Return the URL for our own ZIP streaming endpoint for this project+folder."""
        from core.settings import get_settings
        settings = get_settings()
        base = (settings.asset_api_base_url or "").rstrip("/")
        if not base:
            base = "/api/v1/assets"
        import urllib.parse
        params = urllib.parse.urlencode({"project_name": project_name, "folder_key": folder_key})
        return f"{base}/zip?{params}"

    async def get_versions(
        self,
        project_name: str,
        asset_type: str,
        user: UserContext,
        language: str = "en",
    ) -> list[VersionRow]:
        with tracer.start_as_current_span("asset_registry.get_versions"):
            folder_keys = _ASSET_TYPE_FOLDERS.get(asset_type, [])
            normalised = _normalise(project_name)
            rows: list[VersionRow] = []

            async with AsyncSessionLocal() as session:
                for folder_key in folder_keys:
                    stmt = (
                        select(MarketingCollateralAsset)
                        .where(
                            func.lower(MarketingCollateralAsset.project_title) == normalised,
                            MarketingCollateralAsset.folder_key == folder_key,
                            MarketingCollateralAsset.is_active.is_(True),
                        )
                        .order_by(MarketingCollateralAsset.uploaded_at.desc())
                    )
                    result = await session.execute(stmt)
                    for db_row in result.scalars():
                        rows.append(VersionRow(
                            asset_id=str(db_row.id),
                            project_name=db_row.project_title,
                            version_label=db_row.uploaded_at.strftime("v%Y%m%d") if db_row.uploaded_at else "v1",
                            is_current=True,
                            released_at=db_row.uploaded_at.isoformat() if db_row.uploaded_at else None,
                            release_notes=None,
                            asset_type=asset_type,
                            language=language,
                        ))
            return rows

    async def get_available_languages(
        self,
        project_name: str,
        asset_type: str,
        user: UserContext,
    ) -> list[str]:
        with tracer.start_as_current_span("asset_registry.get_available_languages"):
            # Language detection from filename (e.g. _ar.pdf, _ru.pdf, _chinese.pdf)
            folder_keys = _ASSET_TYPE_FOLDERS.get(asset_type, [])
            normalised = _normalise(project_name)
            languages: set[str] = set()

            async with AsyncSessionLocal() as session:
                for folder_key in folder_keys:
                    stmt = (
                        select(MarketingCollateralAsset.file_name)
                        .where(
                            func.lower(MarketingCollateralAsset.project_title) == normalised,
                            MarketingCollateralAsset.folder_key == folder_key,
                            MarketingCollateralAsset.is_active.is_(True),
                        )
                    )
                    result = await session.execute(stmt)
                    for (fname,) in result:
                        name_lower = fname.lower()
                        if "_ar." in name_lower or name_lower.endswith("_ar"):
                            languages.add("ar")
                        elif "_ru." in name_lower:
                            languages.add("ru")
                        elif "chinese" in name_lower or "_zh" in name_lower:
                            languages.add("zh")
                        else:
                            languages.add("en")

            return sorted(languages) if languages else ["en"]

    async def get_asset_by_share_slug(self, slug: str, tenant_id: str) -> AssetRow:
        with tracer.start_as_current_span("asset_registry.get_asset_by_share_slug"):
            raise NotFoundError(f"Share link '{slug}' not found.")


# Singleton
asset_registry = AssetRegistry()
