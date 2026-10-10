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

from agent import state
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
from core.settings import settings
from core.project_names import normalize_project_name
from core.llm import chat_model_long, stream_text_chunks

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
    # Legacy intent - keep temporarily for compatibility
    "inventory_availability": [],

    # New selective inventory intents
    "inventory_project": ["project_name"],
    "inventory_units": [],
    "inventory_combined": ["project_name"],
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
        entities = parsed.get("entities", {})

        if entities.get("project_name"):
            normalized_project = normalize_project_name(
                entities["project_name"]
            )

            if normalized_project:
                entities["project_name"] = normalized_project

        return (
            parsed.get("intent", "out_of_scope"),
            entities,
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

        current_intent = clarification["intent"]
        missing_param = clarification_next_missing(clarification)

        try:
            new_intent, entities, new_confidence = await _classify(
                query,
                state.get("session_history", []),
            )
        except LLMError:
            logger.error(
                "clarification_classification_failed",
                extra={
                    "query": query[:100],
                    "session_id": state.get("session_id"),
                },
            )

            return {
                "clarification": clarification,
                "path": "clarify",
            }

        # ────────────────────────────────────────────────────────────────
        # CASE 1:
        # User supplied the entity we are currently waiting for.
        #
        # This MUST be checked BEFORE intent switching.
        #
        # Example:
        #
        # current intent = get_brochure
        # missing        = project_name
        # user           = "greenz"
        #
        # classifier may return:
        #   new_intent = get_project_overview
        #   entities = {"project_name": "Greenz"}
        #
        # We DO NOT care about new_intent here.
        # The user answered our clarification question.
        # ────────────────────────────────────────────────────────────────

        if missing_param and entities.get(missing_param):

            updated_cs = clarification_collect(
                clarification,
                missing_param,
                entities[missing_param],
            )

            if clarification_is_complete(updated_cs):

                collected_entities = updated_cs.get("collected", {})
                previous_context = state.get("conversation_context") or {}
                new_context = dict(previous_context)

                if collected_entities.get("project_name"):
                    new_context["project_name"] = collected_entities["project_name"]

                logger.info(
                    "clarification_complete",
                    extra={
                        "intent": updated_cs["intent"],
                        "entities": updated_cs.get("collected", {}),
                        "session_id": state.get("session_id"),
                    },
                )

                return {
                "clarification": updated_cs,
                "intent": updated_cs["intent"],
                "entities": collected_entities,
                "confidence": 1.0,
                "path": updated_cs["intent"],
                "conversation_context": new_context                
                }

            return {
                "clarification": updated_cs,
                "intent": updated_cs["intent"],
                "entities": updated_cs.get("collected", {}),
                "path": "clarify",
            }

        if new_intent == "out_of_scope":
            logger.info(
                "clarification_switched_to_out_of_scope",
                extra={
                    "old_intent": current_intent,
                    "query": query[:100],
                    "session_id": state.get("session_id"),
                },
            )

            return {
                "intent": "out_of_scope",
                "entities": {},
                "confidence": new_confidence,
                "clarification": None,
                "path": "out_of_scope",
            }

        # ────────────────────────────────────────────────────────────────
        # CASE 2:
        # User did NOT provide the missing entity.
        #
        # Now check whether they changed their request.
        #
        # Example:
        #
        # current intent = get_floor_plan
        # missing        = project_name
        # user           = "brochure instead"
        #
        # classifier:
        #   new_intent = get_brochure
        #   project_name = None
        #
        # Therefore replace the old clarification.
        # ────────────────────────────────────────────────────────────────

        if (
            new_intent
            and new_intent != current_intent
            and new_intent != "out_of_scope"
        ):

            required = _REQUIRED_PARAMS.get(new_intent, [])

            collected = {
                key: value
                for key, value in entities.items()
                if value is not None and value != ""
            }

            missing = [
                param
                for param in required
                if not collected.get(param)
            ]

            logger.info(
                "clarification_intent_switched",
                extra={
                    "old_intent": current_intent,
                    "new_intent": new_intent,
                    "entities": entities,
                    "missing": missing,
                    "session_id": state.get("session_id"),
                },
            )

            # New request already contains everything required.
            #
            # Example:
            # "give me Greenz brochure instead"
            if not missing:
                previous_context = state.get("conversation_context") or {}
                new_context = dict(previous_context)

                if collected.get("project_name"):
                    new_context["project_name"] = collected["project_name"]

                return {
                    "intent": new_intent,
                    "entities": collected,
                    "confidence": new_confidence,
                    "clarification": None,
                    "path": new_intent,
                    "conversation_context": new_context,
                }

            # New request still needs clarification.
            #
            # Example:
            # "brochure instead"
            #
            # get_brochure still needs project_name.
            new_clarification = clarification_state_factory(
                intent=new_intent,
                required_params=required,
                collected=collected,
            )

            return {
                "intent": new_intent,
                "entities": collected,
                "confidence": new_confidence,
                "clarification": new_clarification,
                "path": "clarify",
            }

        # ────────────────────────────────────────────────────────────────
        # CASE 3:
        # Neither:
        #   - missing entity was supplied
        #   - nor intent was changed
        #
        # Keep the current clarification active.
        # ────────────────────────────────────────────────────────────────

        logger.info(
            "clarification_still_incomplete",
            extra={
                "intent": clarification["intent"],
                "missing": clarification.get("missing", []),
                "session_id": state.get("session_id"),
            },
        )

        return {
            "clarification": clarification,
            "intent": clarification["intent"],
            "entities": clarification.get("collected", {}),
            "path": "clarify",
        }

    # ── Fresh query — run full intent classification ───────────────────────
    try:
        intent, entities, confidence = await _classify(
            query, state.get("session_history", [])
        )
    except LLMError:
        logger.error("intent_classification_failed", extra={"query": query[:100]})
        return {"intent": "out_of_scope", "entities": {}, "confidence": 0.0}

    previous_context = state.get("conversation_context") or {}
    entities = dict(entities or {})

    extracted_project = entities.get("project_name")
    previous_project = previous_context.get("project_name")

    query_lower = query.strip().lower()

    global_inventory_phrases = (
        "all projects",
        "across all projects",
        "every project",
        "all danube projects",
        "entire portfolio",
        "across projects",
    )

    is_global_inventory = (
        intent in {"inventory_units", "inventory_availability"}
        and any(
            phrase in query_lower
            for phrase in global_inventory_phrases
        )
    )

    # Retain previous project for related follow-up queries,
    # but not for explicitly global inventory requests.
    if (
        not extracted_project
        and previous_project
        and not is_global_inventory
        and intent not in {"out_of_scope"}
    ):
        entities["project_name"] = previous_project

    if is_global_inventory:
        entities["project_name"] = None

    new_context = dict(previous_context)

    if extracted_project:
        new_context["project_name"] = extracted_project

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
            "conversation_context": new_context,
        }

    return {
        "intent": intent,
        "entities": entities,
        "confidence": confidence,
        "clarification": None,
        "conversation_context": new_context,
    }

# ── Out-of-scope node ────────────────────────────────────────────────────────

async def out_of_scope_node(state: AgentState) -> dict:
    """
    Handle requests outside the supported Danube Properties chatbot scope.

    This response is deterministic and intentionally does not call the LLM,
    retrieval layer, or external tools.
    """

    commentary = (
        "I can only assist with Danube Properties-related information, "
        "including projects, inventory, marketing materials, and "
        "Danube platform guidance."
    )

    await adispatch_custom_event(
        "text_response",
        {"text": commentary},
    )


    logger.info(
        "out_of_scope_query",
        extra={
            "query": state.get("query", "")[:100],
            "session_id": state.get("session_id"),
        },
    )


    return {
        "card_html": "",
        "card_type": "out_of_scope",
        "commentary": commentary,
        "clarification": None,
        "path": "out_of_scope",
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

    if (
        clarification.get("clarification_type")
        == "inventory_project"
    ):
        candidates = clarification.get(
            "candidate_projects", []
        )

        # The inventory node has already prepared the
        # clarification commentary.
        # Avoid generating a generic marketing clarification card.
        return {
            "clarification": clarification,
            "path": "await_user",
        }

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
    Handle Salesforce inventory queries.

    Supports:
    - inventory_project: project-level details
    - inventory_units: unit-level details
    - inventory_combined: project and unit details

    Uses the existing commentary streaming mechanism.
    """
    from modules.inventory.service import (
        ResultKind,
        answer_inventory_query,
    )

    entities = state.get("entities") or {}
    query = state.get("query", "")
    inventory_intent = state.get(
        "intent", "inventory_units"
    )

    # Service decides which Salesforce API(s) to call.
    result = await answer_inventory_query(
        entities=entities,
        inventory_intent=inventory_intent,
    )

    # -------------------------------------------
    # Project not found
    # -------------------------------------------
    if result.kind == ResultKind.PROJECT_NOT_FOUND:
        commentary = (
            f"I couldn't find a Danube project matching "
            f"**{result.raw_query_project}**. "
            f"Could you check the spelling or let me know "
            f"which project you meant?"
        )

        return {
            "card_html": "",
            "card_type": "inventory",
            "commentary": commentary,
        }

    # -------------------------------------------
    # Ambiguous project name
    # -------------------------------------------
    if result.kind == ResultKind.AMBIGUOUS_PROJECT:
        candidates = result.candidates or []

        names = ", ".join(
            f"**{name}**"
            for name in candidates
        )

        commentary = (
            f"I found multiple Salesforce projects matching "
            f"**{result.raw_query_project}**: "
            f"{names}. Which project would you like details for?"
        )

        clarification = {
            "intent": inventory_intent,
            "collected": {
                "project_name": result.raw_query_project,
            },
            "missing": ["project_name"],
            "candidate_projects": candidates,
            "clarification_type": "inventory_project",
        }


        await adispatch_custom_event(
            "inventory_commentary",
            {
                "commentary": commentary,
            },
        )



        return {
            "card_html": "",
            "card_type": "inventory",
            "commentary": commentary,
            "clarification": clarification,
            "path": "await_user",
        }


    # -------------------------------------------
    # Salesforce API error
    # -------------------------------------------
    if result.kind == ResultKind.SF_ERROR:
        commentary = (
            "I wasn't able to retrieve live inventory "
            "information right now. "
            "Please try again in a moment."
        )

        logger.warning(
            "inventory_sf_error_surfaced",
            extra={
                "error": result.error_message,
                "session_id": state.get("session_id"),
            },
        )

        return {
            "card_html": "",
            "card_type": "inventory",
            "commentary": commentary,
        }

    # -------------------------------------------
    # Successful Salesforce response
    # -------------------------------------------
    project_label = (
        result.project_name
        or ("all projects" if result.is_global else "")
    )

    filters = result.filters_applied
    filter_desc = _filters_to_text(filters)

    # Build context according to the actual
    # Salesforce result type.
    inventory_context = _inventory_result_context(
        result
    )

    system_prompt = get_prompt(
        "commentary_inventory"
    )

    human_content = (
        f"Inventory intent: {inventory_intent}\n"
        f"Result type: {result.kind.value}\n"
        f"Project searched: "
        f"{project_label or 'All projects'}\n"
        f"Filters applied: "
        f"{filter_desc or 'None'}\n\n"
        f"Salesforce data:\n"
        f"{inventory_context}\n\n"
        f"Broker query: {query}\n\n"
        "Answer using only the supplied Salesforce data. "
        "Do not invent missing values."
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content),
    ]

    commentary_chunks: list[str] = []

    try:
        async for chunk in stream_text_chunks(
            chat_model_long.astream(messages)
        ):
            commentary_chunks.append(chunk)

    except Exception as exc:
        logger.exception(
            "inventory_commentary_failed: %s",
            str(exc),
        )
        raise

    commentary = "".join(commentary_chunks)

    # Fallback if commentary generation fails.
    if not commentary.strip():
        commentary = (
            "I retrieved the inventory information, "
            "but couldn't format the response right now. "
            "Please try again."
        )

    logger.info(
        "inventory_query_complete",
        extra={
            "intent": inventory_intent,
            "result_kind": result.kind.value,
            "unit_count": len(result.units),
            "project": project_label,
            "is_global": result.is_global,
            "filters": filters,
            "session_id": state.get("session_id"),
        },
    )

    return {
        "card_html": "",
        "card_type": "inventory",
        "commentary": commentary,
    }


def _inventory_result_context(result) -> str:
    """
    Convert Salesforce inventory results into
    readable context for the commentary LLM.

    Handles project-only, units-only, and
    combined responses.
    """
    from dataclasses import asdict
    from modules.inventory.service import ResultKind

    sections = []

    # -------------------------------------------
    # Project-level information
    # -------------------------------------------
    if result.kind in {
        ResultKind.PROJECT,
        ResultKind.COMBINED,
    }:
        if result.project is not None:
            project_data = asdict(result.project)

            # Exclude raw/unmapped Salesforce fields
            # to avoid sending unnecessary data.
            project_data.pop("extra", None)

            project_lines = [
                f"{key}: {value}"
                for key, value in project_data.items()
                if value is not None
                and value != ""
            ]

            sections.append(
                "PROJECT DETAILS:\n"
                + "\n".join(project_lines)
            )

    # -------------------------------------------
    # Unit-level information
    # -------------------------------------------
    if result.kind in {
        ResultKind.UNITS,
        ResultKind.COMBINED,
    }:
        units = result.units

        sections.append(
            f"AVAILABLE UNITS FOUND: {len(units)}"
        )

        sections.append(
            "UNIT DETAILS:\n"
            + _units_to_context(
                units,
                result.is_global,
            )
        )

    return "\n\n".join(sections)


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
        (
            f"Unit: {u.unit_code or u.unit_id} | "
            f"Project: {u.project_name or 'Unknown'} | "
            f"Building: {u.building_name or 'Not available'} | "
            f"Type: {u.unit_type or 'Not available'} | "
            f"Bedrooms: "
            f"{u.bedrooms if u.bedrooms is not None else 'Not available'} | "
            f"Price: "
            f"{'AED {:,.0f}'.format(u.price) if u.price is not None else 'Not available'} | "
            f"Floor: "
            f"{u.floor if u.floor is not None else 'Not available'} | "
            f"View: {u.view or 'Not available'} | "
            f"Status: {u.status or 'Not available'}"
        )
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
