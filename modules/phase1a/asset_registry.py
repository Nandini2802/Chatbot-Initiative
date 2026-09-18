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
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
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

    file_name: Optional[str] = None
    file_size_bytes: Optional[int] = None


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

    def _file_matches_language(
        self,
        file_name: str,
        language: str,
    ) -> bool:

        file_name = file_name.lower()
        language = language.lower().strip()

        language_markers = {
            "ar": [
                "_ar.",
                "-ar.",
                "_arabic",
                "-arabic",
                "arabic",
            ],
            "arabic": [
                "_ar.",
                "-ar.",
                "_arabic",
                "-arabic",
                "arabic",
            ],

            "en": [
                "_en.",
                "-en.",
                "_english",
                "-english",
                "english",
            ],
            "english": [
                "_en.",
                "-en.",
                "_english",
                "-english",
                "english",
            ],

            "ru": [
                "_ru.",
                "-ru.",
                "_russian",
                "-russian",
                "russian",
            ],
            "russian": [
                "_ru.",
                "-ru.",
                "_russian",
                "-russian",
                "russian",
            ],

            "zh": [
                "_zh.",
                "-zh.",
                "chinese",
            ],
            "chinese": [
                "_zh.",
                "-zh.",
                "chinese",
            ],
        }

        markers = language_markers.get(
            language,
            [language],
        )

        return any(
            marker in file_name
            for marker in markers
        )

    async def get_live_asset_url(
        self,
        project_name: str,
        asset_type: str,
        language: Optional[str] = None,
    ) -> str:
        """
        Fetch collateral directly from Marketing Collateral API.

        Behaviour:
        1. If language is NOT explicitly specified:
        return dynamic ZIP URL for the requested folder.

        2. If language IS explicitly specified:
        search individual files in that folder for the requested language
        and return that file's direct URL.
        """

        folder_keys = _ASSET_TYPE_FOLDERS.get(asset_type)

        if not folder_keys:
            raise NotFoundError(
                f"Unknown asset type '{asset_type}'."
            )

        try:
            print("Project:", project_name)
            print("Asset Type:", asset_type)
            print("Language:", language)

            response = requests.post(
                _MC_API_URL,
                headers=_MC_API_HEADERS,
                timeout=60,
            )

            response.raise_for_status()

            response_data = response.json()

            projects = (
                response_data
                .get("data", {})
                .get("marketingCollateral", [])
            )

            print("Total projects received:", len(projects))

            matched_project = next(
                (
                    project for project in projects if project.get("project_title", "").strip().lower() == project_name.strip().lower()
                ),
                None,
            )

            if not matched_project:
                raise NotFoundError(
                    f"Project '{project_name}' not found."
                )

            print("Matched Project:",matched_project.get("project_title"))

            # =========================================================
            # CASE 1:
            # Language explicitly specified
            # Search individual files
            # =========================================================

            if language and asset_type in {"brochure", "video"}:

                print(f"Searching individual asset for language: {language}")

                all_asset_rows = matched_project.get("marketing_collateral_row_assets",[])

                # API structure is list[list[asset]]
                assets = []

                for asset_row in all_asset_rows:
                    if isinstance(asset_row, list):
                        assets.extend(asset_row)

                for folder_key in folder_keys:

                    folder_assets = [
                        asset
                        for asset in assets
                        if asset.get("folder_key") == folder_key
                    ]

                    for asset in folder_assets:
                        file_name = asset.get("file_name", "").lower()
                        if self._file_matches_language(
                            file_name,
                            language,
                        ):
                            url = asset.get("url")
                            if url:
                                print("Matched Folder:", folder_key)
                                print("Matched File:",asset.get("file_name"))
                                print("File URL:", url)
                                return url

                raise NotFoundError(
                    f"No {language} {asset_type} found "
                    f"for project '{project_name}'."
                )

            # =========================================================
            # CASE 2:
            # No language specified
            # Return dynamic ZIP URL
            # =========================================================

            print("No language specified.")
            print("Returning folder ZIP URL.")

            folders = (
                matched_project
                .get("download_urls", {})
                .get("folders", {})
            )

            for folder_key in folder_keys:

                folder_data = folders.get(folder_key)

                if folder_data:
                    url = folder_data.get("url")

                    if url:
                        print("Matched Folder:", folder_key)
                        print("ZIP URL:", url)

                        return url

            raise NotFoundError(
                f"No {asset_type} folder found "
                f"for project '{project_name}'."
            )

        except NotFoundError:
            raise

        except requests.RequestException as exc:
            logger.exception(
                "Marketing Collateral API request failed."
            )

            raise NotFoundError(
                "Unable to retrieve marketing collateral."
            ) from exc

        except Exception as exc:
            logger.exception(
                "Unexpected Marketing Collateral API error."
            )

            raise NotFoundError(
                "Unable to retrieve marketing collateral."
            ) from exc

    async def get_asset(
        self,
        project_name: str,
        asset_type: str,
        user: UserContext,
        language: Optional[str] = None,
        unit_type: Optional[str] = None,
    ) -> AssetRow:

        with tracer.start_as_current_span(
            "asset_registry.get_asset"
        ):

            live_url = await self.get_live_asset_url(
                project_name=project_name,
                asset_type=asset_type,
                language=language,
            )

            is_language_specific = (
                bool(language)
                and asset_type in {"brochure", "video"}
            )


            return AssetRow(
                id=f"{project_name}-{asset_type}",
                project_id="",
                project_name=project_name,
                asset_type=asset_type,
                language=language or "all",
                unit_type=unit_type,
                blob_path=live_url,
                content_type=(
                    "application/octet-stream"
                    if is_language_specific
                    else "application/zip"
                ),
                status="active",
                version_label="live",
                is_current_version=True,
                tenant_id=user.tenant_id,
            )

    # async def get_all_assets(
    #     self,
    #     project_name: str,
    #     asset_type: str,
    #     user: UserContext,
    #     language: str = "en",
    #     folder_key: Optional[str] = None,
    # ) -> list[AssetRow]:
    #     """Return all active assets for a project+asset_type (e.g. all floor plan images).
    #     Pass folder_key to restrict to a specific subfolder (e.g. 'interiors')."""
    #     with tracer.start_as_current_span("asset_registry.get_all_assets"):
    #         if folder_key:
    #             folder_keys = [folder_key]
    #         else:
    #             folder_keys = _ASSET_TYPE_FOLDERS.get(asset_type)
    #             if not folder_keys:
    #                 raise NotFoundError(f"Unknown asset type '{asset_type}'.")

    #         normalised = _normalise(project_name)
    #         rows: list[AssetRow] = []

    #         async with AsyncSessionLocal() as session:
    #             for fk in folder_keys:
    #                 stmt = (
    #                     select(MarketingCollateralAsset)
    #                     .where(
    #                         func.lower(MarketingCollateralAsset.project_title) == normalised,
    #                         MarketingCollateralAsset.folder_key == fk,
    #                         MarketingCollateralAsset.is_active.is_(True),
    #                     )
    #                     .order_by(MarketingCollateralAsset.file_name)
    #                 )
    #                 result = await session.execute(stmt)
    #                 for db_row in result.scalars():
    #                     rows.append(_row_to_asset(db_row, asset_type, language, user.tenant_id, None))
    #                 if rows:
    #                     break  # use first folder_key that has results

    #         if not rows:
    #             raise NotFoundError(
    #                 f"No {asset_type} found for project '{project_name}'. "
    #                 "Ensure the sync script has been run: python scripts/sync_marketing_collateral.py"
    #             )
    #         return rows

    async def get_all_assets(
        self,
        project_name: str,
        asset_type: str,
        user: UserContext,
        language: Optional[str] = None,
        folder_key: Optional[str] = None,
    ) -> list[AssetRow]:
        """
        Return all individual assets for the requested asset type
        directly from the live Marketing Collateral API.

        Mainly used when brochure/video is requested without language.
        """

        with tracer.start_as_current_span(
            "asset_registry.get_all_assets"
        ):

            if folder_key:
                folder_keys = [folder_key]
            else:
                folder_keys = _ASSET_TYPE_FOLDERS.get(asset_type)

            if not folder_keys:
                raise NotFoundError(
                    f"Unknown asset type '{asset_type}'."
                )

            try:
                print("Project:", project_name)
                print("Asset Type:", asset_type)
                print("Language:", language)
                print("Folder Keys:", folder_keys)

                # ---------------------------------------------
                # Call Marketing API
                # ---------------------------------------------

                response = requests.post(
                    _MC_API_URL,
                    headers=_MC_API_HEADERS,
                    timeout=60,
                )

                response.raise_for_status()

                response_data = response.json()

                projects = (
                    response_data
                    .get("data", {})
                    .get("marketingCollateral", [])
                )

                print("Total projects received:",len(projects))

                # ---------------------------------------------
                # Match project
                # ---------------------------------------------

                matched_project = next(
                    (
                        project
                        for project in projects
                        if project.get(
                            "project_title", ""
                        ).strip().lower()
                        == project_name.strip().lower()
                    ),
                    None,
                )

                if not matched_project:
                    raise NotFoundError(f"Project '{project_name}' not found.")

                print("Matched Project:",matched_project.get("project_title"))

                # ---------------------------------------------
                # Flatten individual assets
                # ---------------------------------------------

                all_asset_rows = matched_project.get("marketing_collateral_row_assets",[])

                assets = []

                for asset_row in all_asset_rows:
                    if isinstance(asset_row, list):
                        assets.extend(asset_row)

                print("Total individual assets:",len(assets))

                # ---------------------------------------------
                # Search requested folders
                # ---------------------------------------------

                for fk in folder_keys:

                    folder_assets = [
                        asset
                        for asset in assets
                        if asset.get("folder_key") == fk
                    ]

                    print(f"Assets in '{fk}':",len(folder_assets))

                    # Optional language filtering
                    if language:
                        folder_assets = [
                            asset
                            for asset in folder_assets
                            if self._file_matches_language(
                                asset.get("file_name", ""),
                                language,
                            )
                        ]

                    rows: list[AssetRow] = []

                    for asset in folder_assets:

                        url = asset.get("url")

                        if not url:
                            continue

                        mime_type = asset.get(
                            "mime_type",
                            "",
                        )

                        rows.append(
                            AssetRow(
                                id=str(
                                    asset.get(
                                        "marketing_collateral_asset_id"
                                    )
                                    or asset.get("relative_path")
                                    or url
                                ),
                                project_id=str(
                                    matched_project.get(
                                        "project_id",
                                        "",
                                    )
                                ),
                                project_name=matched_project.get(
                                    "project_title",
                                    project_name,
                                ),
                                asset_type=asset_type,
                                language=language or "all",
                                unit_type=None,

                                # Individual file URL
                                blob_path=url,

                                content_type=_MIME_MAP.get(
                                    mime_type,
                                    "application/octet-stream",
                                ),

                                status="active",
                                version_label="live",
                                is_current_version=True,
                                tenant_id=user.tenant_id,
                                file_name=asset.get("file_name"),
                                file_size_bytes=asset.get("size"),

                            )
                        )

                    if rows:
                        print("Matched Folder:", fk)
                        print(
                            "Individual Files Returned:",
                            len(rows),
                        )

                        return rows

                raise NotFoundError(
                    f"No {asset_type} files found "
                    f"for project '{project_name}'."
                )

            except NotFoundError:
                raise

            except requests.RequestException as exc:
                logger.exception(
                    "Marketing Collateral API request failed "
                    "while retrieving individual assets."
                )

                raise NotFoundError(
                    "Unable to retrieve marketing collateral."
                ) from exc

            except Exception as exc:
                logger.exception(
                    "Unexpected Marketing Collateral API error "
                    "while retrieving individual assets."
                )

                raise NotFoundError(
                    "Unable to retrieve marketing collateral."
                ) from exc


    # async def get_folder_zip_url(
    #     self,
    #     project_name: str,
    #     folder_key: str,
    # ) -> Optional[str]:
    #     """Return the URL for our own ZIP streaming endpoint for this project+folder."""
    #     from core.settings import get_settings
    #     settings = get_settings()
    #     base = (settings.asset_api_base_url or "").rstrip("/")
    #     if not base:
    #         base = "/api/v1/assets"
    #     import urllib.parse
    #     params = urllib.parse.urlencode({"project_name": project_name, "folder_key": folder_key})
    #     return f"{base}/zip?{params}"

    async def get_folder_zip_url(
        self,
        project_name: str,
        folder_key: str,
    ) -> str:
        """
        Return the live dynamic ZIP URL for the requested
        project folder directly from Marketing Collateral API.
        """

        with tracer.start_as_current_span(
            "asset_registry.get_folder_zip_url"
        ):

            try:
                print("Project:", project_name)
                print("Folder:", folder_key)

                # Call Marketing Collateral API
                response = requests.post(
                    _MC_API_URL,
                    headers=_MC_API_HEADERS,
                    timeout=60,
                )

                response.raise_for_status()

                response_data = response.json()

                projects = (
                    response_data
                    .get("data", {})
                    .get("marketingCollateral", [])
                )

                print("Total projects received:",len(projects))

                # Find requested project
                matched_project = next(
                    (
                        project
                        for project in projects
                        if project.get(
                            "project_title", ""
                        ).strip().lower()
                        == project_name.strip().lower()
                    ),
                    None,
                )

                if not matched_project:
                    raise NotFoundError(f"Project '{project_name}' not found.")

                print("Matched Project:",matched_project.get("project_title"))

                # Get all available folder download URLs
                folders = (
                    matched_project
                    .get("download_urls", {})
                    .get("folders", {})
                )

                # Find requested folder
                folder_data = folders.get(folder_key)

                if not folder_data:
                    raise NotFoundError(
                        f"Folder '{folder_key}' not found "
                        f"for project '{project_name}'."
                    )

                zip_url = folder_data.get("url")

                if not zip_url:
                    raise NotFoundError(
                        f"No ZIP URL found for folder "
                        f"'{folder_key}' of project "
                        f"'{project_name}'."
                    )

                print("Matched Folder:", folder_key)
                print("ZIP URL:", zip_url)

                return zip_url

            except NotFoundError:
                raise

            except requests.RequestException as exc:
                logger.exception(
                    "Marketing Collateral API request failed "
                    "while retrieving folder ZIP."
                )

                raise NotFoundError(
                    "Unable to retrieve marketing collateral."
                ) from exc

            except Exception as exc:
                logger.exception(
                    "Unexpected Marketing Collateral API error "
                    "while retrieving folder ZIP."
                )

                raise NotFoundError(
                    "Unable to retrieve marketing collateral."
                ) from exc

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
