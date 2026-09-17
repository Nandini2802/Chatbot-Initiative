"""
Master LangGraph graph — compile once, extend per phase by adding nodes/edges.

Phase 1A nodes: intent → clarify / retrieve_asset / retrieve_knowledge / check_version → followups → END

Routing functions live here alongside the graph so the wiring and the logic
that drives it are in the same file. Move them to a separate router.py only
when Phase 2/3 routing grows complex enough to warrant it.
"""
from __future__ import annotations

import logging

from typing import Any

from langgraph.graph import END, StateGraph

from agent.nodes import clarify_node, followups_node, intent_node, inventory_query_node
from agent.state import AgentState, clarification_is_complete
from agent.tools import check_version_node, retrieve_asset_node, retrieve_knowledge_node
from core.settings import settings

logger = logging.getLogger(__name__)

_compiled_graph: Any | None = None


# ── Routing functions ─────────────────────────────────────────────────────────

# Intents handled by each retrieval node
_ASSET_INTENTS = {
    "get_brochure", "get_floor_plan", "get_gallery",
    "get_video", "get_spec_sheet", "get_project_overview",
}
_VERSION_INTENTS = {"check_version", "list_languages"}
_KNOWLEDGE_INTENTS = {"knowledge_query"}
_INVENTORY_INTENTS = {"inventory_availability"}

# Fast-path eligible intents (high-confidence, single-asset, no ambiguity)
FAST_PATH_INTENTS = {
    "get_brochure", "get_floor_plan", "get_video", "check_version", "list_languages", "get_spec_sheet",
}

# Required entities per intent — missing any → clarify
_REQUIRED_ENTITIES: dict[str, list[str]] = {
    "get_brochure":        ["project_name"],
    "get_floor_plan":      ["project_name"],
    "get_gallery":         ["project_name"],
    "get_video":           ["project_name"],
    "check_version":       ["project_name"],
    "list_languages":      ["project_name"],
    "get_spec_sheet":      ["project_name", "unit_type"],
    "get_project_overview":["project_name"],
    "knowledge_query":     [],  # project_name optional — cross-project queries are valid
}


def _intent_to_node(intent: str) -> str:
    if intent in _ASSET_INTENTS:
        return "retrieve_asset"
    if intent in _VERSION_INTENTS:
        return "check_version"
    if intent in _INVENTORY_INTENTS:
        return "inventory_query"
    return "retrieve_knowledge"


def route_after_intent(state: AgentState) -> str:
    """
    Routing decision after the intent node.

    Returns a node name: clarify | retrieve_asset | retrieve_knowledge | check_version.
    """
    intent = state.get("intent", "")
    confidence = state.get("confidence") or 0.0
    entities = state.get("entities") or {}

    if not intent or intent == "out_of_scope":
        return "retrieve_knowledge"

    required = _REQUIRED_ENTITIES.get(intent, [])
    missing = [p for p in required if not entities.get(p)]

    # Low-confidence with NO entities at all → treat as a general/greeting query.
    # Avoids interrogating the user for "HI", "hello", one-word inputs, etc.
    if missing and confidence < 0.45 and not any(entities.get(p) for p in required):
        logger.info(
            "low_confidence_fallback",
            extra={"intent": intent, "confidence": confidence, "query": state.get("query", "")[:60]},
        )
        return "retrieve_knowledge"

    if missing:
        return "clarify"

    # Fast path: high confidence + required entities present
    if intent in FAST_PATH_INTENTS and confidence >= settings.fast_path_threshold:
        return _intent_to_node(intent)

    return _intent_to_node(intent)


def route_after_clarify(state: AgentState) -> str:
    """
    Routing decision after the clarification node.

    Returns a retrieval node name, or "await_user" (which maps to END).
    """
    clarification = state.get("clarification")
    if clarification and clarification_is_complete(clarification):
        return _intent_to_node(clarification["intent"])
    return "await_user"


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_graph(checkpointer) -> Any:
    """Build and compile the Phase 1A LangGraph graph."""
    g = StateGraph(AgentState)

    # ── Phase 1A nodes ────────────────────────────────────────────────────
    g.add_node("intent", intent_node)
    g.add_node("clarify", clarify_node)
    g.add_node("retrieve_asset", retrieve_asset_node)
    g.add_node("retrieve_knowledge", retrieve_knowledge_node)
    g.add_node("check_version", check_version_node)
    g.add_node("inventory_query", inventory_query_node)
    g.add_node("followups", followups_node)

    # ── Entry point ────────────────────────────────────────────────────────
    g.set_entry_point("intent")

    # ── Routing from intent node ───────────────────────────────────────────
    g.add_conditional_edges(
        "intent",
        route_after_intent,
        {
            "clarify": "clarify",
            "retrieve_asset": "retrieve_asset",
            "retrieve_knowledge": "retrieve_knowledge",
            "check_version": "check_version",
            "inventory_query": "inventory_query",
        },
    )

    # ── Clarification loop ─────────────────────────────────────────────────
    g.add_conditional_edges(
        "clarify",
        route_after_clarify,
        {
            "retrieve_asset": "retrieve_asset",
            "retrieve_knowledge": "retrieve_knowledge",
            "check_version": "check_version",
            "inventory_query": "inventory_query",
            "await_user": END,  # graph pauses — resumes on next user turn
        },
    )

    # ── All retrieval paths lead to followups then END ─────────────────────
    g.add_edge("retrieve_asset", "followups")
    g.add_edge("retrieve_knowledge", "followups")
    g.add_edge("check_version", "followups")
    g.add_edge("inventory_query", "followups")
    g.add_edge("followups", END)

    return g.compile(checkpointer=checkpointer)


async def get_graph() -> Any:
    """
    Return the compiled graph, creating it on first call.

    Uses MemorySaver for session persistence (POC — resets on restart).
    Replace with a persistent checkpointer when production DB is available.
    """
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph

    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    _compiled_graph = build_graph(checkpointer)
    logger.info("langgraph_graph_compiled", extra={"phase": "1A"})
    return _compiled_graph
