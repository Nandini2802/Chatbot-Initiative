"""
Asset ingestion pipeline for Phase 1A.

1. Upload binary to blob storage
2. Extract text from PDFs and index chunks into ChromaDB (local persistent)

PostgreSQL removed — no DB writes.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from core.auth import require_ingestion_permission, UserContext
from core.errors import ValidationError
from core.observability import tracer
from core.storage import storage

logger = logging.getLogger(__name__)

_STRIP_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class IngestionRequest:
    project_name: str
    asset_type: str
    language: str
    unit_type: Optional[str]
    file_bytes: bytes
    content_type: str
    filename: str
    version_label: str
    permissions: list[str]
    release_notes: Optional[str] = None


@dataclass
class IngestionResult:
    asset_id: str
    version_id: str
    blob_path: str
    project_id: str
    chunks_indexed: int


def _sanitise_extracted_text(raw: str) -> str:
    """Strip control characters — PDF content is untrusted."""
    return _STRIP_PATTERN.sub("", raw)


async def _extract_text_chunks(
    file_bytes: bytes, content_type: str, chunk_size: int = 800
) -> list[str]:
    if "pdf" not in content_type.lower():
        return []
    try:
        import io
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            text = _sanitise_extracted_text(text)
            if text.strip():
                pages.append(text)

        all_text = "\n\n".join(pages)
        chunks: list[str] = []
        start = 0
        while start < len(all_text):
            end = start + chunk_size
            chunks.append(all_text[start:end])
            start = end - 100
        return [c.strip() for c in chunks if c.strip()]
    except ImportError:
        logger.warning("pypdf_not_installed")
        return []
    except Exception as exc:
        logger.error("pdf_extraction_failed", extra={"error": str(exc)})
        return []


async def _index_chunks(
    chunks: list[str],
    project_id: str,
    project_name: str,
    asset_id: str,
    tenant_id: str,
) -> int:
    if not chunks:
        return 0

    import chromadb
    from core.llm import embeddings_model
    from core.settings import settings
    from modules.phase1a.rag import _COLLECTION_NAME

    client = chromadb.PersistentClient(path=settings.chroma_path)
    collection = client.get_or_create_collection(name=_COLLECTION_NAME)

    vectors = await embeddings_model.aembed_documents(chunks)

    ids, documents, embeddings_list, metadatas = [], [], [], []
    for i, (text, vector) in enumerate(zip(chunks, vectors)):
        chunk_id = str(uuid.uuid4())
        ids.append(chunk_id)
        documents.append(text)
        embeddings_list.append(vector)
        metadatas.append({
            "chunk_id": chunk_id,
            "project_id": project_id,
            "project_name": project_name.lower().strip(),
            "asset_id": asset_id,
            "tenant_id": tenant_id,
            "chunk_index": i,
            "source": f"chunk_{i}",
        })

    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings_list,
        metadatas=metadatas,
    )

    logger.info("chunks_indexed", extra={"count": len(chunks), "project_id": project_id})
    return len(chunks)


def _stable_project_id(project_name: str, tenant_id: str) -> str:
    """Deterministic project ID from name+tenant."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{tenant_id}:{project_name.lower().strip()}"))


async def ingest_asset(req: IngestionRequest, user: UserContext) -> IngestionResult:
    require_ingestion_permission(user.role)

    if not req.file_bytes:
        raise ValidationError("File bytes cannot be empty.")
    if not req.project_name.strip():
        raise ValidationError("project_name is required.")
    if not req.asset_type.strip():
        raise ValidationError("asset_type is required.")

    with tracer.start_as_current_span("ingestion.ingest_asset") as span:
        span.set_attribute("asset_type", req.asset_type)
        span.set_attribute("project_name", req.project_name)
        span.set_attribute("tenant_id", user.tenant_id)

        project_id = _stable_project_id(req.project_name, user.tenant_id)
        asset_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())

        blob_path = (
            f"{user.tenant_id}/{project_id}/{req.asset_type}/"
            f"{req.language}/{req.filename}"
        )

        await storage.upload(
            blob_path=blob_path,
            data=req.file_bytes,
            content_type=req.content_type,
        )

        chunks = await _extract_text_chunks(req.file_bytes, req.content_type)
        chunks_indexed = await _index_chunks(
            chunks=chunks,
            project_id=project_id,
            project_name=req.project_name,
            asset_id=asset_id,
            tenant_id=user.tenant_id,
        )

        logger.info(
            "asset_ingested",
            extra={
                "asset_id": asset_id,
                "project_id": project_id,
                "asset_type": req.asset_type,
                "chunks_indexed": chunks_indexed,
            },
        )

        return IngestionResult(
            asset_id=asset_id,
            version_id=version_id,
            blob_path=blob_path,
            project_id=project_id,
            chunks_indexed=chunks_indexed,
        )
