"""
Knowledge ingestion script — populates Qdrant with text chunks for RAG.

Handles:
  - PDFs  (.pdf)       — text extracted page-by-page with pypdf
  - FAQs  (.json)      — JSON array of {"q": "...", "a": "..."} objects
  - Text  (.txt, .md)  — read as-is, split by paragraph
  - Images/videos      — skipped (no extractable text)

Uses the SAME folder structure as bulk_ingest.py:
    <root>/
        greenz-all-assets/
            brochures/          ← PDFs extracted
            floor-plans/        ← PDFs extracted
            other/              ← PDFs extracted (project overviews)
            amenities/          ← skipped (images)
            exteriors/          ← skipped (images)
            videos/             ← skipped (videos)
            faqs.json           ← FAQ Q&A pairs (anywhere in tree)
            knowledge.txt       ← plain text (anywhere in tree)
        aspirz-all-assets/
            ...

Usage:
    # Dry run (preview chunks, no writes)
    python scripts/ingest_knowledge.py "D:/Data/Marketing Collaterals" --dry-run

    # Ingest all projects
    python scripts/ingest_knowledge.py "D:/Data/Marketing Collaterals"

    # Single project only
    python scripts/ingest_knowledge.py "D:/Data/Marketing Collaterals" --project greenz

    # Re-ingest (wipe existing chunks first)
    python scripts/ingest_knowledge.py "D:/Data/Marketing Collaterals" --wipe
    python scripts/ingest_knowledge.py "D:/Data/Knowledge" --wipe

FAQ JSON format (faqs.json):
    [
        {"q": "Is there a gym?", "a": "Yes, Greenz has a fully equipped gym on level 3."},
        {"q": "Where is Greenz located?", "a": "Greenz is located in Al Furjan, Dubai."}
    ]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Prompt-injection guard ────────────────────────────────────────────────────
_STRIP_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitise(text: str) -> str:
    return _STRIP_PATTERN.sub("", text).strip()


# ── Text extraction ───────────────────────────────────────────────────────────

def _extract_pdf(path: Path) -> list[tuple[str, str]]:
    """Returns list of (text, source_label) tuples — one per page."""
    try:
        import io
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(io.BytesIO(path.read_bytes()))
        results = []
        for i, page in enumerate(reader.pages):
            text = _sanitise(page.extract_text() or "")
            if text:
                results.append((text, f"{path.name}:page{i+1}"))
        if not results:
            logger.warning("  PDF has no extractable text (scanned/image-only): %s", path.name)
        return results
    except ImportError:
        logger.error("  pypdf not installed — run: pip install pypdf")
        return []
    except Exception as exc:
        logger.error("  PDF extraction failed for %s: %s", path.name, exc)
        return []


def _extract_faq(path: Path) -> list[tuple[str, str]]:
    """Parse FAQ JSON — each Q&A becomes one chunk: 'Q: ...\nA: ...'"""
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            logger.warning("  faqs.json must be a JSON array — skipping %s", path.name)
            return []
        results = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            q = _sanitise(str(item.get("q") or item.get("question") or ""))
            a = _sanitise(str(item.get("a") or item.get("answer") or ""))
            if q and a:
                results.append((f"Q: {q}\nA: {a}", f"{path.name}:faq{i+1}"))
            elif a:
                results.append((a, f"{path.name}:faq{i+1}"))
        logger.info("  FAQ %s: %d Q&A pairs", path.name, len(results))
        return results
    except json.JSONDecodeError as exc:
        logger.error("  Invalid JSON in %s: %s", path.name, exc)
        return []


def _extract_text(path: Path) -> list[tuple[str, str]]:
    """Split plain text / markdown by double-newline paragraphs."""
    try:
        raw = _sanitise(path.read_text(encoding="utf-8"))
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
        return [(p, f"{path.name}:para{i+1}") for i, p in enumerate(paragraphs)]
    except Exception as exc:
        logger.error("  Text read failed for %s: %s", path.name, exc)
        return []


def _extract_file(path: Path) -> list[tuple[str, str]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".json":
        return _extract_faq(path)
    if ext in (".txt", ".md", ".markdown"):
        return _extract_text(path)
    logger.warning("  Unsupported file type '%s' — skipping %s", ext, path.name)
    return []


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    """
    Split text into overlapping chunks of ~chunk_size chars.
    For short texts (FAQs, single paragraphs) returns as-is.
    """
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        # Try to break at sentence boundary
        last_period = max(chunk.rfind(". "), chunk.rfind(".\n"))
        if last_period > chunk_size // 2:
            chunk = text[start: start + last_period + 1]
            end = start + last_period + 1
        chunks.append(chunk.strip())
        start = end - overlap
    return [c for c in chunks if c]


# ── Postgres helpers ──────────────────────────────────────────────────────────

async def _resolve_or_create_project(project_name: str, tenant_id: str) -> str:
    """Return project_id, creating the project record if it doesn't exist."""
    from sqlalchemy import select
    from core.db import AsyncSessionLocal
    from modules.phase1a.models import Project

    async with AsyncSessionLocal() as db:
        stmt = (
            select(Project.id)
            .where(Project.tenant_id == tenant_id)
            .where(Project.name_normalized == project_name.lower().strip())
            .where(Project.deleted_at.is_(None))
        )
        row = (await db.execute(stmt)).first()
        if row:
            return row.id

        project = Project(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            name=project_name.strip().title(),
            name_normalized=project_name.lower().strip(),
        )
        db.add(project)
        await db.commit()
        logger.info("  Created project record: %s (%s)", project_name, project.id)
        return project.id


async def _wipe_project_chunks(project_id: str, tenant_id: str) -> None:
    """Delete all Qdrant points and Postgres rows for this project."""
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    from core.settings import settings
    from core.db import AsyncSessionLocal
    from modules.phase1a.models import KnowledgeChunk
    from sqlalchemy import delete

    client = AsyncQdrantClient(url=settings.qdrant_url)
    await client.delete(
        collection_name="danube_knowledge",
        points_selector=Filter(
            must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
        ),
    )
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(KnowledgeChunk)
            .where(KnowledgeChunk.project_id == project_id)
            .where(KnowledgeChunk.tenant_id == tenant_id)
        )
        await db.commit()
    logger.info("  Wiped existing chunks for project_id=%s", project_id)


# ── Qdrant upsert ─────────────────────────────────────────────────────────────

async def _upsert_chunks(
    chunks: list[tuple[str, str]],   # (text, source_label)
    project_id: str,
    tenant_id: str,
    dry_run: bool,
) -> int:
    """Embed and upsert chunks into Qdrant + Postgres knowledge_chunks."""
    if not chunks:
        return 0

    if dry_run:
        for text, source in chunks:
            logger.info("    [DRY-RUN] source=%-30s  chars=%d  preview=%s",
                        source, len(text), text[:80].replace("\n", " "))
        return len(chunks)

    from core.llm import embeddings_model
    from core.settings import settings
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import PointStruct
    from core.db import AsyncSessionLocal
    from modules.phase1a.models import KnowledgeChunk

    client = AsyncQdrantClient(url=settings.qdrant_url)

    texts = [t for t, _ in chunks]
    sources = [s for _, s in chunks]

    # Embed in batches of 50 to avoid token limit
    BATCH = 50
    all_vectors = []
    for i in range(0, len(texts), BATCH):
        batch_vectors = await embeddings_model.aembed_documents(texts[i:i + BATCH])
        all_vectors.extend(batch_vectors)
        logger.info("  Embedded %d/%d chunks...", min(i + BATCH, len(texts)), len(texts))

    points = []
    db_rows = []
    for i, (text, source, vector) in enumerate(zip(texts, sources, all_vectors)):
        point_id = str(uuid.uuid4())
        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "text": text,
                    "project_id": project_id,
                    "tenant_id": tenant_id,
                    "source": source,
                    "chunk_index": i,
                },
            )
        )
        db_rows.append(
            KnowledgeChunk(
                id=str(uuid.uuid4()),
                project_id=project_id,
                asset_id=None,            # knowledge-only, not tied to a specific asset
                tenant_id=tenant_id,
                qdrant_point_id=point_id,
                chunk_text=text,
                chunk_index=i,
            )
        )

    # Upsert Qdrant
    await client.upsert(collection_name="danube_knowledge", points=points)

    # Persist to Postgres
    async with AsyncSessionLocal() as db:
        db.add_all(db_rows)
        await db.commit()

    return len(points)


# ── Setup ─────────────────────────────────────────────────────────────────────

async def _ensure_qdrant_collection() -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, VectorParams
    from core.settings import settings

    COLLECTION = "danube_knowledge"
    VECTOR_SIZE = 3072  # text-embedding-3-large

    client = AsyncQdrantClient(url=settings.qdrant_url)
    existing = {c.name for c in (await client.get_collections()).collections}
    if COLLECTION not in existing:
        await client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '%s'", COLLECTION)
    else:
        logger.info("Qdrant collection '%s' already exists.", COLLECTION)


# ── File type classification ──────────────────────────────────────────────────

# Extensions with extractable text
_TEXT_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".json"}

# Extensions to silently skip (no text to extract)
_SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv",
}


def _project_name_from_folder(folder_name: str) -> str:
    """Same logic as bulk_ingest.py: 'greenz-all-assets' -> 'Greenz'"""
    name = re.sub(r"[-_](all[-_]assets?|assets?|collaterals?)$", "", folder_name, flags=re.I)
    return name.strip("-_ ").title()


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run(
    root: Path,
    tenant_id: str,
    only_project: Optional[str],
    wipe: bool,
    dry_run: bool,
) -> None:
    if not dry_run:
        await _ensure_qdrant_collection()

    total_chunks = 0

    for project_folder in sorted(root.iterdir()):
        if not project_folder.is_dir() or project_folder.name.startswith("."):
            continue

        project_name = _project_name_from_folder(project_folder.name)

        # --project filter: match against display name or raw folder name
        if only_project and only_project.lower() not in (
            project_name.lower(), project_folder.name.lower()
        ):
            continue

        logger.info("")
        logger.info("=== Project: %s  (%s) ===", project_name, project_folder.name)

        if not dry_run:
            project_id = await _resolve_or_create_project(project_name, tenant_id)
            if wipe:
                await _wipe_project_chunks(project_id, tenant_id)
        else:
            project_id = "dry-run-id"

        project_chunks: list[tuple[str, str]] = []
        seen_files = 0
        skipped_files = 0

        # Walk the entire project folder recursively
        for file_path in sorted(project_folder.rglob("*")):
            if not file_path.is_file() or file_path.name.startswith("."):
                continue

            ext = file_path.suffix.lower()

            if ext in _SKIP_EXTENSIONS:
                skipped_files += 1
                continue  # silently skip images/videos

            if ext not in _TEXT_EXTENSIONS:
                logger.warning("  Skipping unsupported type '%s': %s", ext, file_path.name)
                skipped_files += 1
                continue

            seen_files += 1
            # Make source label relative to project folder for readability
            rel = file_path.relative_to(project_folder)
            logger.info("  [%s] %s", ext.lstrip(".").upper(), rel)

            raw_segments = _extract_file(file_path)
            file_chunks: list[tuple[str, str]] = []
            for text, _src in raw_segments:
                for chunk in _chunk(text):
                    file_chunks.append((chunk, str(rel)))

            if file_chunks:
                logger.info("    -> %d chunks", len(file_chunks))
            else:
                logger.warning("    -> 0 chunks (no extractable text)")

            project_chunks.extend(file_chunks)

        logger.info(
            "  Files processed: %d  |  Skipped (images/video): %d  |  Total chunks: %d",
            seen_files, skipped_files, len(project_chunks),
        )

        if project_chunks:
            n = await _upsert_chunks(project_chunks, project_id, tenant_id, dry_run)
            logger.info("  Upserted %d chunks for '%s'", n, project_name)
            total_chunks += n
        else:
            logger.warning(
                "  No extractable text found for '%s'. "
                "Add faqs.json, .txt, or text-bearing PDFs to the folder.",
                project_name,
            )

    logger.info("")
    logger.info("Done. Total chunks upserted: %d", total_chunks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest knowledge documents (PDFs, FAQs, text) into Qdrant for RAG."
    )
    parser.add_argument(
        "knowledge_dir",
        type=Path,
        help="Root folder containing one subfolder per project (e.g. greenz/, aspirz/).",
    )
    parser.add_argument(
        "--tenant", default="danube", help="Tenant ID (default: danube)"
    )
    parser.add_argument(
        "--project", default=None,
        help="Only ingest a single project (folder name, e.g. 'greenz')."
    )
    parser.add_argument(
        "--wipe", action="store_true",
        help="Delete existing Qdrant chunks for the project(s) before re-ingesting."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview chunks without writing anything."
    )
    args = parser.parse_args()

    root = args.knowledge_dir.expanduser().resolve()
    if not root.exists():
        logger.error("Knowledge directory not found: %s", root)
        sys.exit(1)

    asyncio.run(
        _run(
            root=root,
            tenant_id=args.tenant,
            only_project=args.project,
            wipe=args.wipe,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
