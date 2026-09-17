"""
Unit tests for agent/state.py — ClarificationState helpers.
"""
from __future__ import annotations

import pytest

from agent.state import (
    ClarificationState,
    clarification_collect,
    clarification_is_complete,
    clarification_next_missing,
    clarification_state_factory,
)


def test_factory_sets_missing_correctly():
    cs = clarification_state_factory(
        intent="get_floor_plan",
        required_params=["project_name", "unit_type"],
        collected={"project_name": "Olivz"},
    )
    assert cs["missing"] == ["unit_type"]
    assert cs["collected"] == {"project_name": "Olivz"}
    assert cs["intent"] == "get_floor_plan"


def test_next_missing_returns_first():
    cs = ClarificationState(
        intent="get_floor_plan",
        required_params=["project_name", "unit_type"],
        collected={},
        missing=["project_name", "unit_type"],
    )
    assert clarification_next_missing(cs) == "project_name"


def test_next_missing_returns_none_when_complete():
    cs = ClarificationState(
        intent="get_brochure",
        required_params=["project_name"],
        collected={"project_name": "Olivz"},
        missing=[],
    )
    assert clarification_next_missing(cs) is None


def test_is_complete_true():
    cs = ClarificationState(
        intent="get_brochure",
        required_params=["project_name"],
        collected={"project_name": "Olivz"},
        missing=[],
    )
    assert clarification_is_complete(cs) is True


def test_is_complete_false():
    cs = ClarificationState(
        intent="get_floor_plan",
        required_params=["project_name", "unit_type"],
        collected={"project_name": "Sportz"},
        missing=["unit_type"],
    )
    assert clarification_is_complete(cs) is False


def test_collect_updates_missing():
    cs = ClarificationState(
        intent="get_floor_plan",
        required_params=["project_name", "unit_type"],
        collected={"project_name": "Sportz"},
        missing=["unit_type"],
    )
    updated = clarification_collect(cs, "unit_type", "2BR")
    assert clarification_is_complete(updated)
    assert updated["collected"]["unit_type"] == "2BR"
    assert updated["missing"] == []
