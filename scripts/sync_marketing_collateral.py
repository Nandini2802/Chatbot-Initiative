"""
sync_marketing_collateral.py
────────────────────────────
One-time and scheduled sync: fetches all projects + assets from the
middleware marketing_collateral API and writes them to a JSON snapshot
consumed by modules.phase1a.collateral_store.

Usage:
    python scripts/sync_marketing_collateral.py           # full sync
    python scripts/sync_marketing_collateral.py --dry-run # print stats only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.settings import settings

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
                    uploaded_at = datetime.strptime(uploaded_raw, "%Y-%m-%d %H:%M:%S").isoformat()
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
            })
    return rows


def write_snapshot(path: Path, projects: list[dict], assets: list[dict]) -> None:
    """Write atomically so a running app never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "projects": projects,
        "assets": assets,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def sync(dry_run: bool = False) -> None:
    items = fetch_from_api()

    project_rows = [build_project_row(item) for item in items]
    asset_rows = []
    for item in items:
        asset_rows.extend(build_asset_rows(item))

    logger.info(f"Projects to write: {len(project_rows)}")
    logger.info(f"Assets to write:   {len(asset_rows)}")

    if dry_run:
        logger.info("Dry run — no snapshot written.")
        return

    path = Path(settings.collateral_snapshot_path)
    write_snapshot(path, project_rows, asset_rows)
    logger.info(f"Sync complete. Snapshot written to {path.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync marketing collateral to a JSON snapshot")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, no snapshot write")
    args = parser.parse_args()
    sync(dry_run=args.dry_run)
