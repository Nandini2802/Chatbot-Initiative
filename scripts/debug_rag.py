"""Quick RAG debug script — shows what's in Qdrant for a project and what the semantic search retrieves."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main():
    from qdrant_client import AsyncQdrantClient
    from core.settings import settings
    from core.llm import embeddings_model

    client = AsyncQdrantClient(url=settings.qdrant_url)
    COLLECTION = "danube_knowledge"
    QUERY = "is there a Gym at Greenz"
    PROJECT = "greenz"

    # ── 1. Collection stats ────────────────────────────────────────────────
    info = await client.get_collection(COLLECTION)
    print(f"Collection '{COLLECTION}' — total vectors: {info.points_count}")
    print()

    # ── 2. All points in collection (scroll up to 500) ────────────────────
    results, _ = await client.scroll(
        collection_name=COLLECTION,
        scroll_filter=None,
        limit=500,
        with_payload=True,
        with_vectors=False,
    )
    print(f"Scrolled {len(results)} points total")

    greenz_points = [
        r for r in results
        if PROJECT in str(r.payload).lower()
    ]
    print(f"Points mentioning '{PROJECT}': {len(greenz_points)}")
    print()

    if greenz_points:
        print("Sample Greenz payloads:")
        for r in greenz_points[:5]:
            p = r.payload or {}
            print(f"  id={r.id}")
            print(f"    project_id = {p.get('project_id')}")
            print(f"    tenant_id  = {p.get('tenant_id')}")
            print(f"    source     = {p.get('source')}")
            print(f"    text       = {str(p.get('text', ''))[:150]}")
            print()
    else:
        print("  !! No points found for Greenz — project was never ingested into Qdrant !!")
        print()
        # Show what projects ARE in the DB
        project_ids = set()
        for r in results:
            pid = (r.payload or {}).get("project_id")
            if pid:
                project_ids.add(pid)
        print(f"Project IDs currently in Qdrant: {project_ids or 'none'}")
        print()

    # ── 3. Semantic search (what the agent actually queries) ───────────────
    print(f"--- Semantic search: '{QUERY}' ---")
    vector = await embeddings_model.aembed_query(QUERY)
    hits = await client.search(
        collection_name=COLLECTION,
        query_vector=vector,
        limit=5,
        with_payload=True,
    )
    if hits:
        for h in hits:
            p = h.payload or {}
            print(f"  score={h.score:.4f} | project_id={p.get('project_id')} | source={p.get('source')}")
            print(f"    text: {str(p.get('text', ''))[:150]}")
    else:
        print("  No results returned by semantic search.")

    # ── 4. Project ID resolution (what _resolve_project_id returns) ───────
    print()
    print("--- Postgres project_id lookup for 'Greenz' ---")
    from sqlalchemy import select, text
    from core.db import AsyncSessionLocal
    from modules.phase1a.models import Project

    async with AsyncSessionLocal() as db:
        stmt = (
            select(Project.id, Project.name_normalized, Project.display_name)
            .where(Project.tenant_id == "danube")
            .where(Project.deleted_at.is_(None))
        )
        rows = (await db.execute(stmt)).all()
        print(f"Projects in Postgres (tenant=danube): {len(rows)}")
        for row in rows:
            print(f"  id={row.id} | name_normalized={row.name_normalized} | display_name={row.display_name}")

        greenz_match = [r for r in rows if "greenz" in str(r.name_normalized).lower()]
        if greenz_match:
            print(f"\n  'Greenz' resolves to project_id: {greenz_match[0].id}")
        else:
            print("\n  !! 'Greenz' did NOT resolve to a project_id — name_normalized mismatch !!")


asyncio.run(main())
