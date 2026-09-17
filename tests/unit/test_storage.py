"""
Unit tests for core/storage/blob.py — LocalStorageBackend.
"""
from __future__ import annotations

import pytest

from core.storage import LocalStorageBackend
from core.errors import StorageError


@pytest.fixture
def local_storage(tmp_path):
    return LocalStorageBackend(root=str(tmp_path))


@pytest.mark.asyncio
async def test_upload_and_exists(local_storage):
    await local_storage.upload("test/file.pdf", b"hello pdf", "application/pdf")
    assert await local_storage.exists("test/file.pdf")


@pytest.mark.asyncio
async def test_exists_false_for_missing(local_storage):
    assert not await local_storage.exists("nonexistent/file.pdf")


@pytest.mark.asyncio
async def test_signed_url_returns_static_path(local_storage):
    await local_storage.upload("danube/proj/brochure/en/file.pdf", b"data")
    url = await local_storage.get_signed_url("danube/proj/brochure/en/file.pdf", ttl_seconds=900)
    assert url.startswith("/local-assets/")
    assert "danube/proj/brochure/en/file.pdf" in url


@pytest.mark.asyncio
async def test_delete_removes_file(local_storage):
    await local_storage.upload("to_delete.pdf", b"bye")
    assert await local_storage.exists("to_delete.pdf")
    await local_storage.delete("to_delete.pdf")
    assert not await local_storage.exists("to_delete.pdf")


@pytest.mark.asyncio
async def test_path_traversal_rejected(local_storage):
    with pytest.raises(StorageError):
        await local_storage.upload("../../etc/passwd", b"bad")
