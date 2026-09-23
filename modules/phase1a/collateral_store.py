"""
Marketing collateral lookup, backed by a JSON snapshot loaded into memory.

The snapshot is produced by: scripts/sync_marketing_collateral.py
Re-run the sync and restart the app to pick up new collateral.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollateralAsset:
    id: str
    project_id: str
    project_title: str
    folder_key: str
    file_name: str
    original_name: Optional[str]
    relative_path: str
    url: str
    mime_type: Optional[str]
    size_bytes: Optional[int]
    uploaded_at: Optional[datetime]
    is_active: bool


def _parse_uploaded_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _to_asset(raw: dict) -> CollateralAsset:
    relative_path = raw.get("relative_path", "")
    return CollateralAsset(
        id=relative_path,
        project_id=str(raw.get("project_id", "")),
        project_title=raw.get("project_title", ""),
        folder_key=raw.get("folder_key", ""),
        file_name=raw.get("file_name", ""),
        original_name=raw.get("original_name"),
        relative_path=relative_path,
        url=raw.get("url", ""),
        mime_type=raw.get("mime_type"),
        size_bytes=raw.get("size_bytes"),
        uploaded_at=_parse_uploaded_at(raw.get("uploaded_at")),
        is_active=bool(raw.get("is_active", True)),
    )


class CollateralStore:
    """Read-only in-memory index of active assets, keyed by (project_title, folder_key)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._index: dict[tuple[str, str], list[CollateralAsset]] = {}
        self._loaded = False

    def _load(self) -> None:
        index: dict[tuple[str, str], list[CollateralAsset]] = {}

        if not self._path.exists():
            logger.error(
                "collateral_snapshot_missing",
                extra={"path": str(self._path)},
            )
        else:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw.get("assets", []):
                asset = _to_asset(item)
                if not asset.is_active:
                    continue
                key = (asset.project_title.lower().strip(), asset.folder_key)
                index.setdefault(key, []).append(asset)
            logger.info(
                "collateral_snapshot_loaded",
                extra={"path": str(self._path), "assets": sum(len(v) for v in index.values())},
            )

        self._index = index
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if not self._loaded:
                self._load()

    def assets(self, project_title: str, folder_key: str) -> list[CollateralAsset]:
        self._ensure_loaded()
        return list(self._index.get((project_title.lower().strip(), folder_key), []))


collateral_store = CollateralStore(Path(settings.collateral_snapshot_path))
