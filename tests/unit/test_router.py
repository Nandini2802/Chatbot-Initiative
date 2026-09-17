"""
Unit tests for agent routing functions (agent/graph.py)
"""
from __future__ import annotations

import pytest

from agent.graph import route_after_clarify, route_after_intent
from agent.state import ClarificationState
from core.auth import UserContext


def _base_state(
    intent: str,
    confidence: float,
    entities: dict,
    clarification: ClarificationState | None = None,
) -> dict:
    return {
        "query": "test query",
        "user": UserContext(
            broker_id="b1",
            tenant_id="danube",
            role="broker",
            session_id="s1",
            display_name="B",
            email="b@d.ae",
            org_id="d",
        ),
        "session_id": "s1",
        "tenant_id": "danube",
        "messages": [],
        "session_history": [],
        "intent": intent,
        "entities": entities,
        "confidence": confidence,
        "path": None,
        "clarification": clarification,
        "card_html": None,
        "card_type": None,
        "commentary": None,
        "chips": None,
        "genie_conv_id": None,
        "filter_state": None,
    }


class TestRouteAfterIntent:
    def test_high_confidence_brochure_routes_to_retrieve_asset(self):
        state = _base_state("get_brochure", 0.95, {"project_name": "Olivz"})
        assert route_after_intent(state) == "retrieve_asset"

    def test_missing_project_name_triggers_clarification(self):
        state = _base_state("get_brochure", 0.95, {})
        assert route_after_intent(state) == "clarify"

    def test_floor_plan_missing_unit_type_triggers_clarification(self):
        state = _base_state("get_floor_plan", 0.90, {"project_name": "Sportz"})
        assert route_after_intent(state) == "clarify"

    def test_floor_plan_with_all_entities_routes_to_retrieve_asset(self):
        state = _base_state(
            "get_floor_plan", 0.90, {"project_name": "Sportz", "unit_type": "2BR"}
        )
        assert route_after_intent(state) == "retrieve_asset"

    def test_check_version_routes_to_check_version_node(self):
        state = _base_state("check_version", 0.92, {"project_name": "Bayz 101"})
        assert route_after_intent(state) == "check_version"

    def test_knowledge_query_routes_to_retrieve_knowledge(self):
        state = _base_state("knowledge_query", 0.80, {"project_name": "Olivz"})
        assert route_after_intent(state) == "retrieve_knowledge"

    def test_out_of_scope_routes_to_retrieve_knowledge(self):
        state = _base_state("out_of_scope", 0.99, {})
        assert route_after_intent(state) == "retrieve_knowledge"

    def test_low_confidence_with_entities_still_routes(self):
        state = _base_state("get_brochure", 0.50, {"project_name": "Olivz"})
        # Low confidence but entities present — routes to retrieve_asset (agent path)
        assert route_after_intent(state) == "retrieve_asset"


class TestRouteAfterClarify:
    def test_complete_clarification_routes_to_retrieval(self):
        cs = ClarificationState(
            intent="get_brochure",
            required_params=["project_name"],
            collected={"project_name": "Olivz"},
            missing=[],
        )
        state = _base_state("get_brochure", 1.0, {}, clarification=cs)
        assert route_after_clarify(state) == "retrieve_asset"

    def test_incomplete_clarification_routes_to_await_user(self):
        cs = ClarificationState(
            intent="get_brochure",
            required_params=["project_name"],
            collected={},
            missing=["project_name"],
        )
        state = _base_state("get_brochure", 0.0, {}, clarification=cs)
        assert route_after_clarify(state) == "await_user"

    def test_no_clarification_routes_to_await_user(self):
        state = _base_state("get_brochure", 0.0, {}, clarification=None)
        assert route_after_clarify(state) == "await_user"
