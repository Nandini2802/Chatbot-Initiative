"""
RAG retrieval for Phase 1A — ChromaDB (replaces Qdrant).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import chromadb

from core.settings import settings
from core.llm import embeddings_model, chat_model
from core.observability import tracer

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "danube_knowledge"
_chroma: chromadb.ClientAPI | None = None


def _get_chroma() -> chromadb.ClientAPI:
    global _chroma
    if _chroma is None:
        _chroma = chromadb.PersistentClient(path=settings.chroma_path)
    return _chroma


async def hybrid_retrieve(
    query: str,
    project_name: Optional[str],
    tenant_id: str,
    top_k: int = 5,
) -> list[dict]:
    """Dense vector search via ChromaDB."""
    with tracer.start_as_current_span("rag.hybrid_retrieve") as span:
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("project_name", project_name or "")

        try:
            client = _get_chroma()
            collection = client.get_or_create_collection(name=_COLLECTION_NAME)

            vector = await embeddings_model.aembed_query(query)

            if project_name:
                where: dict = {
                    "$and": [
                        {"tenant_id": {"$eq": tenant_id}},
                        {"project_name": {"$eq": project_name.lower().strip()}},
                    ]
                }
            else:
                where = {"tenant_id": {"$eq": tenant_id}}

            results = collection.query(
                query_embeddings=[vector],
                n_results=top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )

            chunks: list[dict] = []
            if results["documents"]:
                for doc, meta, dist in zip(
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0],
                ):
                    chunks.append({
                        "id": meta.get("chunk_id", ""),
                        "text": doc,
                        "source": meta.get("source", "KB"),
                        "score": max(0.0, 1.0 - dist),
                    })

            logger.info(
                "chroma_retrieve_complete",
                extra={"count": len(chunks), "tenant_id": tenant_id},
            )
            return chunks[:top_k]

        except Exception as exc:
            logger.warning(
                "chroma_search_failed",
                extra={"error": str(exc), "tenant_id": tenant_id},
            )
            return []


async def _expand_query(query: str, project_name: Optional[str]) -> list[str]:
    """
    Use the LLM to generate 2-3 semantically varied sub-queries from the original.
    Returns the original query plus generated variants (deduped).
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from core.prompts import get_prompt

    try:
        system_prompt = get_prompt("query_expansion")
        context = f"Project context: {project_name}\n" if project_name else ""
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"{context}Query: {query}"),
        ]
        # Suppress callbacks so sub-query tokens don't leak into the SSE stream
        response = await chat_model.ainvoke(messages, config={"callbacks": []})
        raw = response.content.strip()

        import json
        sub_queries = json.loads(raw)
        if not isinstance(sub_queries, list):
            raise ValueError("Expected list")

        # Always include the original; keep unique, limit to 4 total
        seen: set[str] = set()
        result: list[str] = []
        for q in [query] + sub_queries:
            q = q.strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                result.append(q)
            if len(result) >= 4:
                break
        return result

    except Exception as exc:
        logger.warning("query_expansion_failed", extra={"error": str(exc)})
        return [query]


async def multi_query_retrieve(
    query: str,
    project_name: Optional[str],
    tenant_id: str,
    top_k: int = 5,
) -> list[dict]:
    """
    Multi-query RAG:
    1. Expand the query into sub-queries via LLM.
    2. Run ChromaDB vector search for each sub-query in parallel.
    3. Deduplicate by chunk_id, re-rank by best score, return top_k.
    """
    with tracer.start_as_current_span("rag.multi_query_retrieve") as span:
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("project_name", project_name or "")

        # Step 1: expand query
        sub_queries = await _expand_query(query, project_name)
        span.set_attribute("sub_query_count", len(sub_queries))
        logger.info(
            "multi_query_expanded",
            extra={"original": query[:80], "sub_queries": sub_queries, "tenant_id": tenant_id},
        )

        # Step 2: run all sub-queries in parallel — fetch more per query then merge
        per_query_k = max(top_k, 8)
        tasks = [
            hybrid_retrieve(q, project_name, tenant_id, top_k=per_query_k)
            for q in sub_queries
        ]
        results_per_query = await asyncio.gather(*tasks, return_exceptions=True)

        # Step 3: deduplicate by chunk_id, keep best score per chunk
        best: dict[str, dict] = {}
        for result in results_per_query:
            if isinstance(result, Exception):
                logger.warning("sub_query_failed", extra={"error": str(result)})
                continue
            for chunk in result:
                cid = chunk.get("id") or chunk.get("source", "")
                if cid not in best or chunk["score"] > best[cid]["score"]:
                    best[cid] = chunk

        merged = sorted(best.values(), key=lambda c: c["score"], reverse=True)

        logger.info(
            "multi_query_complete",
            extra={"unique_chunks": len(merged), "returned": min(len(merged), top_k)},
        )
        return merged[:top_k]
