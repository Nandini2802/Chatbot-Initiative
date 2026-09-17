"""
One-time Qdrant collection setup script.

Run once per environment before ingesting any assets:
  python scripts/setup_qdrant.py

Creates the danube_knowledge collection with text-embedding-3-large dimensions (3072).
"""
from __future__ import annotations

import asyncio
import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

from core.settings import settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "danube_knowledge"
VECTOR_SIZE = 3072  # text-embedding-3-large output dimensions


async def setup_collection() -> None:
    client = AsyncQdrantClient(url=settings.qdrant_url)

    collections = await client.get_collections()
    existing = {c.name for c in collections.collections}

    if COLLECTION_NAME in existing:
        logger.info("collection_exists", extra={"collection": COLLECTION_NAME})
        return

    await client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    logger.info("collection_created", extra={"collection": COLLECTION_NAME, "size": VECTOR_SIZE})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(setup_collection())
