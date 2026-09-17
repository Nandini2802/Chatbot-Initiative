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
from core.settings import StorageBackend, settings

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


# ── Azure Blob (prod) backend ─────────────────────────────────────────────────

class AzureBlobBackend(StorageBackendABC):
    def __init__(self, account_url: str, container_name: str) -> None:
        # Azure SDK imported lazily so it never loads in local dev
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas
        from datetime import datetime, timedelta, timezone

        self._generate_blob_sas = generate_blob_sas
        self._BlobSasPermissions = BlobSasPermissions
        self._datetime = datetime
        self._timezone = timezone
        self._timedelta = timedelta

        credential = DefaultAzureCredential()
        self._client = BlobServiceClient(account_url=account_url, credential=credential)
        self._container = container_name

    async def upload(self, blob_path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        try:
            blob_client = self._client.get_blob_client(container=self._container, blob=blob_path)
            blob_client.upload_blob(data, overwrite=True, content_settings={"content_type": content_type})
            logger.info("azure_blob_upload", extra={"blob_path": blob_path, "size_bytes": len(data)})
            return blob_path
        except Exception as exc:
            raise StorageError(f"Azure upload failed for {blob_path}: {exc}") from exc

    async def get_signed_url(self, blob_path: str, ttl_seconds: int) -> str:
        try:
            account_name = self._client.account_name
            expiry = self._datetime.now(self._timezone.utc) + self._timedelta(seconds=ttl_seconds)
            sas = self._generate_blob_sas(
                account_name=account_name,
                container_name=self._container,
                blob_name=blob_path,
                account_key=self._client.credential.account_key,
                permission=self._BlobSasPermissions(read=True),
                expiry=expiry,
            )
            return f"https://{account_name}.blob.core.windows.net/{self._container}/{blob_path}?{sas}"
        except Exception as exc:
            raise StorageError(f"Failed to generate signed URL for {blob_path}: {exc}") from exc

    async def exists(self, blob_path: str) -> bool:
        return self._client.get_blob_client(container=self._container, blob=blob_path).exists()

    async def delete(self, blob_path: str) -> None:
        self._client.get_blob_client(container=self._container, blob=blob_path).delete_blob()
        logger.info("azure_blob_delete", extra={"blob_path": blob_path})


# ── Singleton ─────────────────────────────────────────────────────────────────

def _create_backend() -> StorageBackendABC:
    if settings.storage_backend == StorageBackend.azure:
        return AzureBlobBackend(
            account_url=settings.azure_storage_account_url,
            container_name=settings.azure_storage_container_name,
        )
    return LocalStorageBackend(root=settings.local_storage_path)


storage: StorageBackendABC = _create_backend()


async def get_signed_url(blob_path: str, ttl_seconds: int | None = None) -> str:
    """Return a signed URL. Raw blob paths are never returned to clients."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.signed_url_ttl_seconds
    return await storage.get_signed_url(blob_path, ttl_seconds=ttl)
