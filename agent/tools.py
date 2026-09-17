"""
Agent retrieval tools — asset retrieval, version check, and knowledge RAG.

All three retrieval nodes live here. Split into separate files when Phase 2/3
adds new retrieval logic significant enough to warrant its own file.
"""
from __future__ import annotations

import logging

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import HumanMessage, SystemMessage

from agent.state import AgentState
from core.prompts import get_prompt
from core.errors import NotFoundError
from core.llm import chat_model, chat_model_long, stream_text_chunks
from core.observability import record_card_served, tracer

logger = logging.getLogger(__name__)


# ── retrieve_asset node ───────────────────────────────────────────────────────
# Handles: get_brochure, get_floor_plan, get_gallery, get_video,
#          get_spec_sheet, get_project_overview

def _get_card_builder():
    # Lazy import to avoid circular dependency at module load time
    from modules.phase1a import card_builder
    return card_builder


_COMMENTARY_PROMPT_KEY: dict[str, str] = {
    "get_brochure":         "commentary_brochure",
    "get_floor_plan":       "commentary_floor_plan",
    "get_gallery":          "commentary_gallery",
    "get_video":            "commentary_video",
    "get_spec_sheet":       "commentary_brochure",
    "get_project_overview": "commentary_project_overview",
}

# Intents that should also run RAG from the vector DB to enrich commentary
_RAG_ENRICHED_INTENTS = {"get_project_overview"}


async def retrieve_asset_node(state: AgentState) -> dict:
    """
    Fetch asset from DB, build card, emit html_block, stream commentary.

    RBAC and tenant_id are enforced inside card_builder/asset_registry —
    never post-filter here in Python.
    """
    with tracer.start_as_current_span("retrieve_asset_node") as span:
        intent = state.get("intent", "")
        entities = state.get("entities") or {}
        user = state["user"]

        span.set_attribute("intent", intent)
        span.set_attribute("broker_id", user.broker_id)
        span.set_attribute("tenant_id", user.tenant_id)
        span.set_attribute("project_name", entities.get("project_name", ""))

        builder = _get_card_builder()

        try:
            _card_data, card_type, html = await builder.build_card(
                intent=intent,
                entities=entities,
                user=user,
            )
        except NotFoundError as exc:
            logger.warning(
                "asset_not_found",
                extra={
                    "intent": intent,
                    "entities": entities,
                    "session_id": state.get("session_id"),
                },
            )
            await adispatch_custom_event(
                "html_block",
                {
                    "html": f'<div class="not-found-card"><p>{exc}</p></div>',
                    "card_type": "not_found",
                },
            )
            return {"card_html": "", "card_type": "not_found", "commentary": ""}

        except Exception as exc:
            span.record_exception(exc)
            # Detect DB / storage connectivity failures and surface a clear message
            exc_str = str(exc).lower()
            if any(k in exc_str for k in ("connection refused", "could not connect", "connection reset", "operationalerror", "asyncpg")):
                logger.error(
                    "retrieve_asset_db_unavailable",
                    extra={"error": str(exc), "intent": intent},
                )
                await adispatch_custom_event(
                    "html_block",
                    {
                        "html": '<div class="not-found-card"><p>The asset database is temporarily unavailable. Please try again shortly.</p></div>',
                        "card_type": "error",
                    },
                )
                return {"card_html": "", "card_type": "error", "commentary": ""}
            logger.error(
                "retrieve_asset_failed",
                extra={"error": str(exc), "intent": intent},
            )
            raise

        await adispatch_custom_event("html_block", {"html": html, "card_type": card_type})
        record_card_served(card_type=card_type, tenant_id=user.tenant_id)

        # Optionally enrich commentary with RAG context from Qdrant
        knowledge_context = ""
        if intent in _RAG_ENRICHED_INTENTS:
            try:
                from modules.phase1a.rag import hybrid_retrieve
                chunks = await hybrid_retrieve(
                    query=state["query"],
                    project_name=entities.get("project_name"),
                    tenant_id=user.tenant_id,
                    top_k=5,
                )
                if chunks:
                    knowledge_context = "\n\n---\n\n".join(
                        f"[Source: {c.get('source', 'KB')}]\n{c['text']}" for c in chunks
                    )
                    logger.info(
                        "rag_enriched",
                        extra={"chunks": len(chunks), "intent": intent},
                    )
            except Exception as exc:
                logger.warning("rag_enrichment_failed", extra={"error": str(exc)})

        # Stream LLM commentary — tokens are picked up by astream_events on_chat_model_stream
        prompt_key = _COMMENTARY_PROMPT_KEY.get(intent, "commentary_brochure")
        system_prompt = get_prompt(prompt_key)
        card_context = (
            f"Project: {entities.get('project_name', 'Unknown')}\nCard type: {card_type}"
        )
        human_content = f"{card_context}\nBroker query: {state['query']}"
        if knowledge_context:
            human_content += f"\n\nKnowledge base context:\n{knowledge_context}"

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_content),
        ]

        commentary_chunks: list[str] = []
        # project_overview generates a markdown bullet list — needs more tokens
        _llm = chat_model_long if intent == "get_project_overview" else chat_model
        async for text in stream_text_chunks(_llm.astream(messages)):
            commentary_chunks.append(text)
        commentary = "".join(commentary_chunks)

        logger.info(
            "asset_retrieved",
            extra={
                "card_type": card_type,
                "intent": intent,
                "session_id": state.get("session_id"),
            },
        )

        return {"card_html": html, "card_type": card_type, "commentary": commentary}


# ── check_version node ────────────────────────────────────────────────────────
# Handles: check_version, list_languages

async def check_version_node(state: AgentState) -> dict:
    """Check version status or list available language versions for an asset."""
    with tracer.start_as_current_span("check_version_node") as span:
        intent = state.get("intent", "check_version")
        entities = state.get("entities") or {}
        user = state["user"]

        span.set_attribute("intent", intent)
        span.set_attribute("broker_id", user.broker_id)
        span.set_attribute("tenant_id", user.tenant_id)

        from modules.phase1a.card_builder import build_language_list_card, build_version_card

        try:
            if intent == "list_languages":
                html, card_type = await build_language_list_card(entities, user)
            else:
                html, card_type = await build_version_card(entities, user)
        except Exception as exc:
            span.record_exception(exc)
            logger.error(
                "check_version_failed",
                extra={"error": str(exc), "intent": intent},
            )
            raise

        await adispatch_custom_event("html_block", {"html": html, "card_type": card_type})
        record_card_served(card_type=card_type, tenant_id=user.tenant_id)

        system_prompt = get_prompt("commentary_version_check")
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    f"Project: {entities.get('project_name', 'Unknown')}\n"
                    f"Query: {state['query']}"
                )
            ),
        ]

        commentary_chunks: list[str] = []
        async for text in stream_text_chunks(chat_model.astream(messages)):
            commentary_chunks.append(text)
        commentary = "".join(commentary_chunks)

        logger.info(
            "version_checked",
            extra={"card_type": card_type, "session_id": state.get("session_id")},
        )

        return {"card_html": html, "card_type": card_type, "commentary": commentary}


# ── retrieve_knowledge node ───────────────────────────────────────────────────
# Handles: knowledge_query and all out-of-scope fallbacks

async def retrieve_knowledge_node(state: AgentState) -> dict:
    """
    Retrieve relevant project knowledge and stream a RAG answer.

    Uses hybrid search: dense (Qdrant vector) + sparse (PostgreSQL FTS).
    No card emitted — knowledge answers are text-only.
    """
    with tracer.start_as_current_span("retrieve_knowledge_node") as span:
        query = state["query"]
        entities = state.get("entities") or {}
        user = state["user"]

        span.set_attribute("broker_id", user.broker_id)
        span.set_attribute("tenant_id", user.tenant_id)
        span.set_attribute("project_name", entities.get("project_name", ""))

        from modules.phase1a.rag import multi_query_retrieve

        try:
            chunks = await multi_query_retrieve(
                query=query,
                project_name=entities.get("project_name"),
                tenant_id=user.tenant_id,
                top_k=8 if entities.get("project_name") else 12,
            )
        except Exception as exc:
            span.record_exception(exc)
            logger.error(
                "knowledge_retrieve_failed",
                extra={"error": str(exc), "query": query[:100]},
            )
            chunks = []

        # Always produce context — fall back to general-knowledge signal
        if not chunks:
            knowledge_context = ""
            context_source = "general"
        else:
            knowledge_context = "\n\n---\n\n".join(
                f"[Source: {c.get('source', 'KB')}]\n{c['text']}" for c in chunks
            )
            context_source = "kb"

        system_prompt = get_prompt("commentary_knowledge")

        # Build rich context for the LLM — includes intent signal and conversation history
        intent = state.get("intent", "")
        history: list[dict] = state.get("session_history") or []
        history_text = ""
        if history:
            history_text = "\n".join(
                f"{m['role'].upper()}: {m['content']}" for m in history[-6:]
            )

        human_parts = []
        if intent and intent not in ("out_of_scope", "knowledge_query"):
            human_parts.append(f"Intent signal: {intent}")
        if context_source == "kb":
            human_parts.append(f"Knowledge context (from Danube knowledge base):\n{knowledge_context}")
        else:
            human_parts.append(
                "Knowledge context: Not found in knowledge base. "
                "Answer using your general knowledge about UAE real estate and Danube Properties. "
                "Clearly note when information is based on general knowledge."
            )
        if history_text:
            human_parts.append(f"Recent conversation:\n{history_text}")
        human_parts.append(f"Broker message: {query}")

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content="\n\n".join(human_parts)),
        ]

        commentary_chunks: list[str] = []
        async for text in stream_text_chunks(chat_model_long.astream(messages)):
            commentary_chunks.append(text)
        commentary = "".join(commentary_chunks)

        logger.info(
            "knowledge_retrieved",
            extra={"chunks_found": len(chunks), "session_id": state.get("session_id")},
        )

        return {"card_html": "", "card_type": "knowledge", "commentary": commentary}
