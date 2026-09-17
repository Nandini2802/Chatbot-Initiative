"""
Asset endpoints — share link creation and resolution.

Share links are stored in-memory (POC only, resets on restart).
"""
from __future__ import annotations

import io
import logging
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.auth import UserContext
from core.db.session import AsyncSessionLocal
from core.db.models import MarketingCollateralAsset
from core.errors import NotFoundError
from modules.phase1a.asset_registry import asset_registry, _ASSET_TYPE_FOLDERS, _normalise
from sqlalchemy import select, func

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory share link store — POC only, resets on restart
_share_links: dict[str, dict] = {}


def _get_user(request: Request) -> UserContext:
    return request.state.user


class CreateShareLinkRequest(BaseModel):
    asset_id: str
    expires_in_seconds: int = Field(default=86400, ge=300, le=604800)


class CreateShareLinkResponse(BaseModel):
    slug: str
    share_url: str


class ResolveShareLinkResponse(BaseModel):
    download_url: str
    project: str
    asset_type: str
    version_label: Optional[str]


@router.post("/share", response_model=CreateShareLinkResponse)
async def create_share_link(
    body: CreateShareLinkRequest,
    request: Request,
    user: UserContext = Depends(_get_user),
) -> CreateShareLinkResponse:
    slug = uuid.uuid4().hex[:16]
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in_seconds)
    _share_links[slug] = {
        "asset_id": body.asset_id,
        "tenant_id": user.tenant_id,
        "expires_at": expires_at,
        "created_by": user.broker_id,
    }
    logger.info("share_link_created", extra={"slug": slug, "asset_id": body.asset_id})
    return CreateShareLinkResponse(slug=slug, share_url=f"/api/v1/assets/share/{slug}")


@router.get("/share/{slug}", response_model=ResolveShareLinkResponse)
async def resolve_share_link(
    slug: str,
    request: Request,
    user: UserContext = Depends(_get_user),
) -> ResolveShareLinkResponse:
    from core.storage import get_signed_url

    link = _share_links.get(slug)
    if not link:
        raise NotFoundError(f"Share link '{slug}' not found.")
    if link["tenant_id"] != user.tenant_id:
        raise NotFoundError(f"Share link '{slug}' not found.")
    if datetime.now(timezone.utc) > link["expires_at"]:
        raise NotFoundError(f"Share link '{slug}' has expired.")

    row = await asset_registry.get_asset_by_share_slug(slug=slug, tenant_id=user.tenant_id)
    signed_url = await get_signed_url(row.blob_path)

    logger.info("share_link_resolved", extra={"slug": slug, "broker_id": user.broker_id})
    return ResolveShareLinkResponse(
        download_url=signed_url,
        project=row.project_name,
        asset_type=row.asset_type,
        version_label=row.version_label,
    )


# ── ZIP download ──────────────────────────────────────────────────────────────

@router.get("/zip")
async def download_folder_zip(
    project_name: str = Query(..., description="Project title, e.g. 'Greenz'"),
    folder_key: str = Query(..., description="Folder key, e.g. 'interiors', 'floor-plans'"),
    request: Request = None,
    user: UserContext = Depends(_get_user),
) -> StreamingResponse:
    """Stream a ZIP of all active assets in a project folder."""
    normalised = _normalise(project_name)

    async with AsyncSessionLocal() as session:
        stmt = (
            select(MarketingCollateralAsset)
            .where(
                func.lower(MarketingCollateralAsset.project_title) == normalised,
                MarketingCollateralAsset.folder_key == folder_key,
                MarketingCollateralAsset.is_active.is_(True),
            )
            .order_by(MarketingCollateralAsset.file_name)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    if not rows:
        raise NotFoundError(f"No assets found for project '{project_name}', folder '{folder_key}'.")

    async def _generate_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                for row in rows:
                    try:
                        resp = await client.get(row.url)
                        resp.raise_for_status()
                        zf.writestr(row.file_name, resp.content)
                    except Exception as exc:
                        logger.warning("zip_skip_file", extra={"file": row.file_name, "error": str(exc)})
        buf.seek(0)
        yield buf.read()

    safe_project = project_name.replace(" ", "_")
    safe_folder = folder_key.replace("/", "-")
    filename = f"{safe_project}_{safe_folder}.zip"

    logger.info("zip_download", extra={"project": project_name, "folder": folder_key, "broker_id": user.broker_id, "count": len(rows)})
    return StreamingResponse(
        _generate_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
