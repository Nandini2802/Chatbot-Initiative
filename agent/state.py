"""
AgentState — single state schema for all phases.

Never add ad-hoc keys outside this TypedDict.
When adding Phase 2/3 fields, add Optional fields only — never rename or remove.
"""
from __future__ import annotations

from typing import Annotated, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from core.auth import UserContext


class ClarificationState(TypedDict, total=False):
    """Tracks clarification progress across turns."""

    intent: str                    # the intent we are clarifying for
    required_params: list[str]     # params required for this intent
    collected: dict[str, str]      # params already collected
    missing: list[str]             # params still needed


class AgentState(TypedDict, total=False):
    # ── Core query context ────────────────────────────────────────────────
    query: str
    user: UserContext
    session_id: str
    tenant_id: str

    # ── Conversation history ───────────────────────────────────────────────
    messages: Annotated[list, add_messages]
    session_history: list[dict]

    # ── Intent classification output ──────────────────────────────────────
    intent: Optional[str]
    entities: Optional[dict]
    confidence: Optional[float]
    path: Optional[str]            # "fast" | "agent" | "await_user"

    # ── Clarification loop state ───────────────────────────────────────────
    clarification: Optional[ClarificationState]

    # ── Card / response output ────────────────────────────────────────────
    card_html: Optional[str]
    card_type: Optional[str]
    commentary: Optional[str]
    chips: Optional[list[str]]

    # ── Phase 2 placeholders (None in Phase 1A) ───────────────────────────
    genie_conv_id: Optional[str]
    filter_state: Optional[dict]


def clarification_state_factory(
    intent: str,
    required_params: list[str],
    collected: dict[str, str] | None = None,
) -> ClarificationState:
    """Create a fresh ClarificationState for a given intent."""
    collected = collected or {}
    return ClarificationState(
        intent=intent,
        required_params=required_params,
        collected=collected,
        missing=[p for p in required_params if p not in collected],
    )


def clarification_next_missing(cs: ClarificationState) -> str | None:
    """Return the next missing parameter, or None if complete."""
    missing = cs.get("missing", [])
    return missing[0] if missing else None


def clarification_is_complete(cs: ClarificationState) -> bool:
    return len(cs.get("missing", [])) == 0


def clarification_collect(
    cs: ClarificationState, param: str, value: str
) -> ClarificationState:
    """Return a new ClarificationState with param collected."""
    collected = {**cs.get("collected", {}), param: value}
    missing = [p for p in cs.get("required_params", []) if p not in collected]
    return ClarificationState(
        intent=cs["intent"],
        required_params=cs.get("required_params", []),
        collected=collected,
        missing=missing,
    )
