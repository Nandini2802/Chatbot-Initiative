"""
Admin endpoints — asset ingestion, project management.

All endpoints require admin or rm role (enforced via RBAC in ingestion.py).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional

from core.auth import UserContext
from modules.phase1a.ingestion import IngestionRequest, IngestionResult, ingest_asset

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_user(request: Request) -> UserContext:
    return request.state.user


class IngestResponse(BaseModel):
    asset_id: str
    version_id: str
    project_id: str
    chunks_indexed: int


@router.post("/ingest", response_model=IngestResponse)
async def ingest_asset_endpoint(
    request: Request,
    file: UploadFile = File(...),
    project_name: str = Form(...),
    asset_type: str = Form(...),
    language: str = Form(default="en"),
    unit_type: Optional[str] = Form(default=None),
    version_label: str = Form(...),
    permissions: str = Form(default="broker,rm,admin"),
    release_notes: Optional[str] = Form(default=None),
    user: UserContext = Depends(_get_user),
) -> IngestResponse:
    """
    Ingest an asset file into the platform.

    Requires admin or rm role. Uploads to blob storage, creates/updates
    Asset and AssetVersion records, and indexes text chunks for RAG.
    """
    file_bytes = await file.read()

    req = IngestionRequest(
        project_name=project_name,
        asset_type=asset_type,
        language=language,
        unit_type=unit_type,
        file_bytes=file_bytes,
        content_type=file.content_type or "application/octet-stream",
        filename=file.filename or f"{asset_type}_{language}",
        version_label=version_label,
        permissions=[p.strip() for p in permissions.split(",")],
        release_notes=release_notes,
    )

    result: IngestionResult = await ingest_asset(req, user)

    logger.info(
        "admin_ingest_complete",
        extra={
            "asset_id": result.asset_id,
            "project_name": project_name,
            "broker_id": user.broker_id,
        },
    )

    return IngestResponse(
        asset_id=result.asset_id,
        version_id=result.version_id,
        project_id=result.project_id,
        chunks_indexed=result.chunks_indexed,
    )
