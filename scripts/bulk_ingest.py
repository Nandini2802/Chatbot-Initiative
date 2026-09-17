"""
Bulk text ingestion into ChromaDB.

Walks danube_projects/<ProjectName>/*.txt and indexes every file into ChromaDB.

Folder layout:
    <projects-root>/
        Greenz/
            Greenz_Description.txt
            Greenz_FAQs.txt
            Greenz_Project_Facts.txt
            Greenz_Amenities_Featured.txt
            Greenz_Amenity_Labels.txt
            Greenz_Location_Proximity.txt
        Aspirz/
            ...

File suffix -> asset_type mapping:
    _Description          -> project_overview
    _Project_Facts        -> project_overview
    _FAQs                 -> faq
    _Amenities_Featured   -> amenities
    _Amenity_Labels       -> amenities
    _Location_Proximity   -> location

Usage:
    python scripts/bulk_ingest.py --projects-root "D:/Codes/Danube one Chatbot/danube_projects"
    python scripts/bulk_ingest.py --projects-root "D:/..." --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Map filename suffix (after project name, case-insensitive) -> asset_type
_SUFFIX_TO_ASSET_TYPE: list[tuple[str, str]] = [
    ("_faqs",                  "faq"),
    ("_description",           "project_overview"),
    ("_project_facts",         "project_overview"),
    ("_amenities_featured",    "amenities"),
    ("_amenity_labels",        "amenities"),
    ("_location_proximity",    "location"),
]


def infer_asset_type(stem: str) -> str:
    """Infer asset_type from filename stem, e.g. 'Greenz_FAQs' -> 'faq'."""
    lower = stem.lower()
    for suffix, asset_type in _SUFFIX_TO_ASSET_TYPE:
        if lower.endswith(suffix):
            return asset_type
    return "general"


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end].strip())
        start = end - overlap
    return [c for c in chunks if c]


async def index_txt_file(
    collection,
    embeddings_model,
    txt_path: Path,
    project_name: str,
    asset_type: str,
    tenant_id: str,
    dry_run: bool,
) -> int:
    text = txt_path.read_text(encoding="utf-8", errors="ignore")
    chunks = chunk_text(text)

    if not chunks:
        logger.warning("  SKIP  empty after chunking: %s", txt_path.name)
        return 0

    if dry_run:
        logger.info(
            "  DRY   %-20s | %-18s | %s | %d chunks",
            project_name, asset_type, txt_path.name, len(chunks),
        )
        return len(chunks)

    vectors = await embeddings_model.aembed_documents(chunks)

    ids, documents, embeddings_list, metadatas = [], [], [], []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        chunk_id = str(uuid.uuid4())
        ids.append(chunk_id)
        documents.append(chunk)
        embeddings_list.append(vector)
        metadatas.append({
            "chunk_id":     chunk_id,
            "project_name": project_name.lower().strip(),
            "asset_type":   asset_type,
            "language":     "en",
            "tenant_id":    tenant_id,
            "source_file":  txt_path.name,
            "chunk_index":  i,
            "source":       f"{project_name}/{txt_path.name}#{i}",
        })

    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings_list,
        metadatas=metadatas,
    )

    logger.info(
        "  OK    %-20s | %-18s | %s -> %d chunks",
        project_name, asset_type, txt_path.name, len(chunks),
    )
    return len(chunks)


async def run(projects_root: Path, tenant_id: str, dry_run: bool):
    from core.settings import settings
    from core.llm import embeddings_model
    import chromadb
    from modules.phase1a.rag import _COLLECTION_NAME

    client = chromadb.PersistentClient(path=settings.chroma_path)
    collection = client.get_or_create_collection(name=_COLLECTION_NAME)

    if not projects_root.exists():
        logger.error("Projects root not found: %s", projects_root)
        sys.exit(1)

    total_files = 0
    total_chunks = 0

    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name  # e.g. "Greenz", "BAYZ101"

        txt_files = sorted(project_dir.glob("*.txt"))
        if not txt_files:
            logger.debug("  --    No .txt files in %s, skipping.", project_name)
            continue

        logger.info("Project: %s (%d files)", project_name, len(txt_files))

        for txt_path in txt_files:
            asset_type = infer_asset_type(txt_path.stem)
            chunks_indexed = await index_txt_file(
                collection=collection,
                embeddings_model=embeddings_model,
                txt_path=txt_path,
                project_name=project_name,
                asset_type=asset_type,
                tenant_id=tenant_id,
                dry_run=dry_run,
            )
            total_files += 1
            total_chunks += chunks_indexed

    logger.info("")
    if dry_run:
        logger.info(
            "DRY RUN complete -- %d file(s), ~%d chunks (nothing written)",
            total_files, total_chunks,
        )
    else:
        logger.info(
            "Done -- %d file(s), %d chunks indexed into ChromaDB at '%s'",
            total_files, total_chunks, settings.chroma_path,
        )


def main():
    parser = argparse.ArgumentParser(description="Bulk index .txt files into ChromaDB")
    parser.add_argument(
        "--projects-root",
        default="D:/Codes/Danube one Chatbot/danube_projects",
        help="Path to the folder containing one subfolder per project",
    )
    parser.add_argument("--tenant", default="danube")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    projects_root = Path(args.projects_root).resolve()
    asyncio.run(run(projects_root=projects_root, tenant_id=args.tenant, dry_run=args.dry_run))


if __name__ == "__main__":
    main()