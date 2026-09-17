"""
Agent nodes — intent classification, clarification, and follow-up chips.

All three nodes live here. Split into separate files when Phase 2/3 adds
significant new node logic that warrants its own file.
"""
from __future__ import annotations

import json
import logging

from langchain_core.callbacks import adispatch_custom_event
from langchain_core.messages import HumanMessage, SystemMessage

from agent.state import (
    AgentState,
    ClarificationState,
    clarification_collect,
    clarification_is_complete,
    clarification_next_missing,
    clarification_state_factory,
)
from core.prompts import get_prompt
from core.errors import LLMError
from core.llm import chat_model
from core.observability import record_clarification

logger = logging.getLogger(__name__)


# ── Intent node ───────────────────────────────────────────────────────────────

# Required params per intent — missing any triggers the clarification node
_REQUIRED_PARAMS: dict[str, list[str]] = {
    "get_brochure":          ["project_name"],
    "get_floor_plan":        ["project_name"],
    "get_gallery":           ["project_name"],
    "get_video":             ["project_name"],
    "check_version":         ["project_name"],
    "list_languages":        ["project_name"],
    "get_spec_sheet":        ["project_name", "unit_type"],
    "get_project_overview":  ["project_name"],
    "knowledge_query":       [],  # project_name optional — cross-project queries are valid
    "inventory_availability": [],  # all entities optional — bare 'what's available' is valid
}


async def _classify(query: str, history: list[dict]) -> tuple[str, dict, float]:
    """Call the LLM to classify intent and extract entities."""
    system_prompt = get_prompt("intent_classification")

    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in history[-6:]
    )
    user_content = (
        f"Conversation history:\n{history_text}\n\nBroker query: {query}"
        if history_text
        else f"Broker query: {query}"
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    response = await chat_model.ainvoke(messages)
    raw = response.content.strip()

    try:
        parsed = json.loads(raw)
        return (
            parsed.get("intent", "out_of_scope"),
            parsed.get("entities", {}),
            float(parsed.get("confidence", 0.5)),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(
            "intent_parse_failed",
            extra={"raw_response": raw[:200], "error": str(exc)},
        )
        raise LLMError(f"Failed to parse intent classification response: {exc}") from exc


async def intent_node(state: AgentState) -> dict:
    """
    Intent classification node.

    Handles both fresh queries and follow-up turns with active clarification.
    Returns a dict of state updates — never mutates state in place.
    """
    query = state["query"]
    clarification: ClarificationState | None = state.get("clarification")

    # ── Follow-up turn: active clarification in progress ─────────────────
    if clarification and not clarification_is_complete(clarification):
        try:
            _intent, entities, _conf = await _classify(
                query, state.get("session_history", [])
            )
        except LLMError:
            # Treat the whole query as the value for the next missing param
            missing = clarification_next_missing(clarification)
            entities = {missing: query.strip()} if missing else {}

        updated_cs = clarification
        for param in clarification.get("missing", []):
            if param in entities and entities[param]:
                updated_cs = clarification_collect(updated_cs, param, entities[param])

        if clarification_is_complete(updated_cs):
            logger.info(
                "clarification_complete",
                extra={"intent": updated_cs["intent"], "session_id": state.get("session_id")},
            )
            return {
                "clarification": updated_cs,
                "intent": updated_cs["intent"],
                "entities": updated_cs.get("collected", {}),
                "confidence": 1.0,
                "path": updated_cs["intent"],
            }

        logger.info(
            "clarification_still_incomplete",
            extra={"intent": updated_cs["intent"], "missing": updated_cs.get("missing", [])},
        )
        return {"clarification": updated_cs, "path": "clarify"}

    # ── Fresh query — run full intent classification ───────────────────────
    try:
        intent, entities, confidence = await _classify(
            query, state.get("session_history", [])
        )
    except LLMError:
        logger.error("intent_classification_failed", extra={"query": query[:100]})
        return {"intent": "out_of_scope", "entities": {}, "confidence": 0.0}

    logger.info(
        "intent_classified",
        extra={
            "intent": intent,
            "confidence": confidence,
            "session_id": state.get("session_id"),
        },
    )

    required = _REQUIRED_PARAMS.get(intent, [])
    missing = [p for p in required if not entities.get(p)]

    if missing:
        cs = clarification_state_factory(
            intent=intent,
            required_params=required,
            collected={p: v for p, v in entities.items() if v},
        )
        return {
            "intent": intent,
            "entities": entities,
            "confidence": confidence,
            "clarification": cs,
            "path": "clarify",
        }

    return {
        "intent": intent,
        "entities": entities,
        "confidence": confidence,
        "clarification": None,
    }


# ── Clarification node ────────────────────────────────────────────────────────

_PARAM_PROMPTS: dict[str, str] = {
    "project_name": "Which Danube project are you looking for? (e.g. Olivz, Sportz, Bayz 101)",
    "unit_type":    "Which unit type would you like? (e.g. Studio, 1BR, 2BR, 3BR)",
    "language":     "Which language version would you like? (e.g. English, Arabic, Russian)",
    "version":      "Which version are you looking for, or would you like the latest?",
}
_DEFAULT_PROMPT = "Could you provide more details to help me find the right material?"


def _build_clarification_html(missing_param: str, project_name: str | None) -> str:
    prompt = _PARAM_PROMPTS.get(missing_param, _DEFAULT_PROMPT)
    project_hint = f" for {project_name}" if project_name else ""
    return (
        f'<div class="clarification-card" data-param="{missing_param}">'
        f'<p class="clarification-prompt">{prompt}{project_hint}</p>'
        f"</div>"
    )


async def clarify_node(state: AgentState) -> dict:
    """
    Clarification node — emits a clarification card and returns to END.

    The graph completes this turn naturally. The broker's next message
    resumes from the checkpointer-persisted ClarificationState.
    No interrupt() in Phase 1A — HITL is deferred to Phase 2.
    """
    clarification = state.get("clarification")
    if not clarification:
        logger.error(
            "clarify_node_no_clarification_state",
            extra={"session_id": state.get("session_id")},
        )
        return {"path": "await_user"}

    missing_param = clarification_next_missing(clarification)
    if not missing_param:
        logger.warning(
            "clarify_node_no_missing_param",
            extra={"session_id": state.get("session_id")},
        )
        return {"path": "await_user"}

    intent = clarification.get("intent", "unknown")
    collected = clarification.get("collected", {})
    project_name = collected.get("project_name")

    record_clarification(intent=intent, tenant_id=state.get("tenant_id", "unknown"))

    card_type = f"clarification_{missing_param}"
    html = _build_clarification_html(missing_param, project_name)

    await adispatch_custom_event(
        "html_block",
        {"html": html, "card_type": card_type},
    )

    logger.info(
        "clarification_emitted",
        extra={
            "intent": intent,
            "missing_param": missing_param,
            "session_id": state.get("session_id"),
        },
    )

    return {
        "clarification": clarification,
        "path": "await_user",
    }


# ── Inventory query node ───────────────────────────────────────────────────────
# Handles: inventory_availability

async def inventory_query_node(state: AgentState) -> dict:
    """
    Live inventory availability query against Salesforce.

    Returns plain streamed text — no HTML card. The LLM formats the full
    answer from the raw unit data so the output feels like a natural
    conversation rather than a data dump.
    """
    from modules.inventory.service import ResultKind, answer_inventory_query

    entities = state.get("entities") or {}
    query = state.get("query", "")

    result = await answer_inventory_query(entities)

    # ── Project not found ─────────────────────────────────────────────────
    if result.kind == ResultKind.PROJECT_NOT_FOUND:
        commentary = (
            f"I couldn't find a Danube project matching "
            f"**{result.raw_query_project}**. "
            f"Could you check the spelling or let me know which project you meant?"
        )
        return {"card_html": "", "card_type": "inventory", "commentary": commentary}

    # ── Ambiguous project name ────────────────────────────────────────────
    if result.kind == ResultKind.AMBIGUOUS_PROJECT:
        names = ", ".join(f"**{c}**" for c in result.candidates)
        commentary = (
            f"I found a few projects that could match **{result.raw_query_project}**: "
            f"{names}. Which one did you mean?"
        )
        return {"card_html": "", "card_type": "inventory", "commentary": commentary}

    # ── Salesforce error ──────────────────────────────────────────────────
    if result.kind == ResultKind.SF_ERROR:
        commentary = (
            "I wasn't able to retrieve live inventory right now. "
            "Please try again in a moment."
        )
        logger.warning(
            "inventory_sf_error_surfaced",
            extra={"error": result.error_message, "session_id": state.get("session_id")},
        )
        return {"card_html": "", "card_type": "inventory", "commentary": commentary}

    # ── Normal units result — stream LLM-formatted text ───────────────────
    units = result.units
    project_label = result.project_name or ("all projects" if result.is_global else "")
    filters = result.filters_applied
    unit_summary = _units_to_context(units, result.is_global)
    filter_desc = _filters_to_text(filters)

    system_prompt = get_prompt("commentary_inventory")
    human_content = (
        f"Project searched: {project_label or 'All (global)'}\n"
        f"Filters applied: {filter_desc or 'None'}\n"
        f"Available units found: {len(units)}\n\n"
        f"Unit data:\n{unit_summary}\n\n"
        f"Broker query: {query}"
    )
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content),
    ]

    commentary_chunks: list[str] = []
    try:
        async for text in stream_text_chunks(chat_model_long.astream(messages)):
            commentary_chunks.append(text)
    except Exception as exc:
        logger.warning(
            "inventory_commentary_failed",
            extra={"error": str(exc), "session_id": state.get("session_id")},
        )
    commentary = "".join(commentary_chunks)

    logger.info(
        "inventory_query_complete",
        extra={
            "unit_count": len(units),
            "project": project_label,
            "is_global": result.is_global,
            "filters": filters,
            "session_id": state.get("session_id"),
        },
    )

    return {"card_html": "", "card_type": "inventory", "commentary": commentary}


def _build_inventory_card(
    units: list,
    project_label: str,
    filters: dict,
    is_global: bool,
) -> str:
    """Build a compact HTML card summarising inventory results."""
    from collections import defaultdict

    count = len(units)
    title = f"Available units — {project_label}" if project_label else "Available units (all projects)"
    filter_tags = " ".join(
        f'<span class="filter-tag">{k}: {v}</span>' for k, v in filters.items()
    )

    if count == 0:
        body = "<p class='inv-empty'>No units match the selected criteria.</p>"
    elif is_global:
        # Global query — group by project
        by_project: dict[str, list] = defaultdict(list)
        for u in units:
            by_project[u.project_name or "Unknown"].append(u)
        rows = "".join(
            f"<tr><td>{proj}</td><td>{len(us)}</td></tr>"
            for proj, us in sorted(by_project.items())
        )
        body = (
            "<table class='inv-table'>"
            "<thead><tr><th>Project</th><th>Available</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    elif count > 50:
        # Project-specific but very large — summarise by bedroom/category
        from collections import Counter
        breakdown: Counter = Counter(
            (u.bedrooms if u.bedrooms is not None else "?", u.category or "?")
            for u in units
        )
        rows = "".join(
            f"<tr><td>{beds} BR</td><td>{cat}</td><td>{cnt}</td></tr>"
            for (beds, cat), cnt in sorted(breakdown.items(), key=lambda x: (str(x[0][0]), x[0][1]))
        )
        body = (
            "<table class='inv-table'>"
            "<thead><tr><th>Bedrooms</th><th>Category</th><th>Available</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            "<p class='inv-hint'>Use bedroom or category filters to narrow down.</p>"
        )
    else:
        # Project-specific, manageable count — list individual units
        rows = "".join(
            f"<tr>"
            f"<td>{u.unit_code or u.unit_id}</td>"
            f"<td>{u.unit_type or '—'}</td>"
            f"<td>{u.bedrooms if u.bedrooms is not None else '—'} BR</td>"
            f"<td>{f'AED {u.price:,.0f}' if u.price else '—'}</td>"
            f"<td>Floor {u.floor or '—'}</td>"
            f"</tr>"
            for u in units
        )
        body = (
            "<table class='inv-table'>"
            "<thead><tr><th>Unit</th><th>Type</th><th>Beds</th><th>Price</th><th>Floor</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    return (
        f'<div class="inventory-card">'
        f'<h3 class="inv-title">{title}</h3>'
        f'<div class="inv-count">{count} unit{"s" if count != 1 else ""} found</div>'
        f'<div class="inv-filters">{filter_tags}</div>'
        f"{body}"
        f"</div>"
    )


def _units_to_context(units: list, is_global: bool) -> str:
    """Serialize units to a compact text block for the LLM prompt."""
    if not units:
        return "(none)"

    if is_global and len(units) > 30:
        # Global query with many units — group by project + bedrooms for brevity
        from collections import Counter
        counter: Counter = Counter(
            (u.project_name or "Unknown", u.bedrooms) for u in units
        )
        lines = [
            f"  {proj} — {beds if beds is not None else '?'} BR: {cnt} units"
            for (proj, beds), cnt in sorted(
                counter.items(),
                key=lambda x: (x[0][0], x[0][1] if x[0][1] is not None else 9999),
            )
        ]
        return "\n".join(lines)

    # Project-specific query — always list every unit individually
    return "\n".join(
        f"  {u.unit_code or u.unit_id} | {u.unit_type or '?'} | "
        f"{u.bedrooms if u.bedrooms is not None else '?'}BR | "
        f"{'AED {:,.0f}'.format(u.price) if u.price is not None else 'Price TBC'} | "
        f"Floor {u.floor or '?'} | View: {u.view or '—'}"
        for u in units
    )


def _filters_to_text(filters: dict) -> str:
    if not filters:
        return ""
    return ", ".join(f"{k}={v}" for k, v in filters.items())


# ── Follow-up chips node ──────────────────────────────────────────────────────

_DEFAULT_CHIPS = [
    "Show me more projects",
    "Get the floor plan",
    "Check latest version",
]


async def followups_node(state: AgentState) -> dict:
    """
    Generate 3 follow-up chips and emit them via SSE.

    Falls back to default chips on any failure — never blocks the stream.
    """
    try:
        system_prompt = get_prompt("followup_chips")
        context = (
            f"Intent: {state.get('intent', 'unknown')}\n"
            f"Card type: {state.get('card_type', 'unknown')}\n"
            f"Project: {state.get('entities', {}).get('project_name', 'unknown')}\n"
            f"Original query: {state.get('query', '')}"
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=context),
        ]

        response = await chat_model.ainvoke(messages)
        raw = response.content.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        chips: list[str] = json.loads(raw)
        if not isinstance(chips, list) or not chips:
            chips = _DEFAULT_CHIPS

        # Clamp to 3 chips, max 60 chars each
        chips = [str(c).strip()[:60] for c in chips[:3]]

    except Exception as exc:
        logger.warning(
            "followup_chips_fallback",
            extra={"error": str(exc), "session_id": state.get("session_id")},
        )
        chips = _DEFAULT_CHIPS

    await adispatch_custom_event("followups", {"chips": chips})

    logger.info(
        "followups_emitted",
        extra={"chips": chips, "session_id": state.get("session_id")},
    )

    # Append this turn to session_history so future turns have conversation context.
    # Kept to the last 20 turns to avoid unbounded growth.
    history: list[dict] = list(state.get("session_history") or [])
    history.append({"role": "user", "content": state.get("query", "")})
    if state.get("commentary"):
        history.append({"role": "assistant", "content": state["commentary"]})
    history = history[-20:]

    return {"chips": chips, "session_history": history}
