"""
sync_marketing_collateral.py
────────────────────────────
One-time and scheduled sync: fetches all projects + assets from the
middleware marketing_collateral API and upserts them into Postgres.

Usage:
    python scripts/sync_marketing_collateral.py           # full sync
    python scripts/sync_marketing_collateral.py --dry-run # print stats only
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

# Ensure the shared package directory is on the path when run directly.
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))


from core.db.session import AsyncSessionLocal, engine, Base
from core.db.models import MarketingCollateralProject, MarketingCollateralAsset

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

API_URL = "https://middleware.danubeproperties.com/api/marketing_collateral"
API_HEADERS = {
    "x_api_key": "hffghfgVb$)7yV;e0N<{e1t@,_S{_LNovryojjUpJTO#-sD4gE*HHcpBX(Q%KEK6ATy6lQK",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}


def fetch_from_api() -> list[dict]:
    logger.info("Fetching marketing collateral from API...")
    resp = requests.post(API_URL, headers=API_HEADERS, timeout=60)
    resp.raise_for_status()
    items = resp.json()["data"]["marketingCollateral"]
    logger.info(f"Fetched {len(items)} projects from API.")
    return items


def build_project_row(item: dict) -> dict:
    return {
        "marketing_collateral_id": item["marketing_collateral_id"],
        "project_id": item["project_id"],
        "project_title": item.get("project_title", ""),
        "project_location": item.get("project_location_subtitle"),
        "thumb_full": item.get("project_thumb_full"),
        "thumb_large": item.get("project_thumb_large"),
        "thumb_med": item.get("project_thumb_med"),
        "thumb_thumb": item.get("project_thumb_thumb"),
        "synced_at": datetime.now(timezone.utc),
    }


def build_asset_rows(item: dict) -> list[dict]:
    rows = []
    for row_group in item.get("marketing_collateral_row_assets", []):
        for asset in row_group:
            if not isinstance(asset, dict):
                continue
            uploaded_raw = asset.get("uploaded_at")
            uploaded_at = None
            if uploaded_raw:
                try:
                    uploaded_at = datetime.strptime(uploaded_raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            rows.append({
                "project_id": item["project_id"],
                "project_title": item.get("project_title", ""),
                "folder_key": asset.get("folder_key", ""),
                "file_name": asset.get("file_name", ""),
                "original_name": asset.get("original_name"),
                "relative_path": asset.get("relative_path", ""),
                "url": asset.get("url", ""),
                "mime_type": asset.get("mime_type"),
                "size_bytes": asset.get("size"),
                "uploaded_at": uploaded_at,
                "is_active": True,
                "synced_at": datetime.now(timezone.utc),
            })
    return rows


async def ensure_tables() -> None:
    """Create tables if they don't exist (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def sync(dry_run: bool = False) -> None:
    items = fetch_from_api()

    project_rows = [build_project_row(item) for item in items]
    asset_rows = []
    for item in items:
        asset_rows.extend(build_asset_rows(item))

    logger.info(f"Projects to upsert: {len(project_rows)}")
    logger.info(f"Assets to upsert:   {len(asset_rows)}")

    if dry_run:
        logger.info("Dry run — no DB writes.")
        return

    await ensure_tables()

    async with AsyncSessionLocal() as session:
        async with session.begin():
            # ── Upsert projects ───────────────────────────────────────────
            stmt = pg_insert(MarketingCollateralProject).values(project_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["marketing_collateral_id"],
                set_={
                    "project_title":   stmt.excluded.project_title,
                    "project_location": stmt.excluded.project_location,
                    "thumb_full":      stmt.excluded.thumb_full,
                    "thumb_large":     stmt.excluded.thumb_large,
                    "thumb_med":       stmt.excluded.thumb_med,
                    "thumb_thumb":     stmt.excluded.thumb_thumb,
                    "synced_at":       stmt.excluded.synced_at,
                },
            )
            await session.execute(stmt)

            # ── Mark all existing assets inactive, then upsert fresh data ─
            await session.execute(
                text("UPDATE marketing_collateral_assets SET is_active = false")
            )
            stmt = pg_insert(MarketingCollateralAsset).values(asset_rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_mc_asset_relative_path",
                set_={
                    "url":           stmt.excluded.url,
                    "mime_type":     stmt.excluded.mime_type,
                    "size_bytes":    stmt.excluded.size_bytes,
                    "uploaded_at":   stmt.excluded.uploaded_at,
                    "is_active":     True,
                    "synced_at":     stmt.excluded.synced_at,
                },
            )
            await session.execute(stmt)

    logger.info("Sync complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync marketing collateral to Postgres")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, no DB writes")
    args = parser.parse_args()
    asyncio.run(sync(dry_run=args.dry_run))
