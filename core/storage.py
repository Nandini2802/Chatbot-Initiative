"""
Storage — blob backend abstraction and signed URL generation.

Business logic never imports Azure SDK directly. Use the `storage` singleton.
Raw blob paths are never returned to clients — always use get_signed_url().
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from core.errors import StorageError
from core.settings import settings

logger = logging.getLogger(__name__)


# ── Abstract interface ────────────────────────────────────────────────────────

class StorageBackendABC(ABC):
    @abstractmethod
    async def upload(self, blob_path: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...

    @abstractmethod
    async def get_signed_url(self, blob_path: str, ttl_seconds: int) -> str: ...

    @abstractmethod
    async def exists(self, blob_path: str) -> bool: ...

    @abstractmethod
    async def delete(self, blob_path: str) -> None: ...


# ── Local (dev) backend ───────────────────────────────────────────────────────

class LocalStorageBackend(StorageBackendABC):
    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _abs(self, blob_path: str) -> Path:
        resolved = (self._root / blob_path).resolve()
        if not str(resolved).startswith(str(self._root.resolve())):
            raise StorageError(f"Path traversal attempt: {blob_path}")
        return resolved

    async def upload(self, blob_path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        abs_path = self._abs(blob_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)
        logger.info("local_storage_upload", extra={"blob_path": blob_path, "size_bytes": len(data)})
        return blob_path

    async def get_signed_url(self, blob_path: str, ttl_seconds: int) -> str:
        # Pass through absolute URLs (e.g. dummy/placeholder assets)
        if blob_path.startswith("http://") or blob_path.startswith("https://"):
            return blob_path
        return f"/local-assets/{blob_path.lstrip('/')}"

    async def exists(self, blob_path: str) -> bool:
        return self._abs(blob_path).exists()

    async def delete(self, blob_path: str) -> None:
        path = self._abs(blob_path)
        if path.exists():
            path.unlink()
            logger.info("local_storage_delete", extra={"blob_path": blob_path})


# ── Singleton ─────────────────────────────────────────────────────────────────
# Marketing collateral URLs are always absolute middleware CDN links (passed
# through unmodified below); local disk storage is only used for internally
# uploaded knowledge-base documents (see modules/phase1a/ingestion.py).

storage: StorageBackendABC = LocalStorageBackend(root=settings.local_storage_path)


async def get_signed_url(blob_path: str, ttl_seconds: int | None = None) -> str:
    """Return a signed URL. Raw blob paths are never returned to clients."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.signed_url_ttl_seconds
    return await storage.get_signed_url(blob_path, ttl_seconds=ttl)
