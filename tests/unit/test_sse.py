"""
Unit tests for core/streaming/sse.py
"""
from __future__ import annotations

import json

import pytest

from core.sse import (
    DONE,
    EventType,
    done,
    error,
    followups,
    html_block,
    status,
    token,
)


def _parse(sse_str: str) -> dict:
    """Parse an SSE frame into a dict."""
    assert sse_str.startswith("data: ")
    assert sse_str.endswith("\n\n")
    return json.loads(sse_str[6:].strip())


def test_html_block():
    result = _parse(html_block("<div>test</div>", "brochure"))
    assert result["type"] == EventType.HTML_BLOCK
    assert result["html"] == "<div>test</div>"
    assert result["card_type"] == "brochure"


def test_token():
    result = _parse(token("Hello"))
    assert result["type"] == EventType.TOKEN
    assert result["text"] == "Hello"


def test_followups():
    chips = ["Get floor plan", "Check version", "View gallery"]
    result = _parse(followups(chips))
    assert result["type"] == EventType.FOLLOWUPS
    assert result["chips"] == chips


def test_error():
    result = _parse(error("Something went wrong.", "AGENT_ERROR"))
    assert result["type"] == EventType.ERROR
    assert result["message"] == "Something went wrong."
    assert result["code"] == "AGENT_ERROR"


def test_done_no_run_id():
    result = _parse(done())
    assert result["type"] == EventType.DONE
    assert "run_id" not in result


def test_done_with_run_id():
    result = _parse(done(run_id="abc-123"))
    assert result["type"] == EventType.DONE
    assert result["run_id"] == "abc-123"


def test_status():
    result = _parse(status("Processing..."))
    assert result["type"] == EventType.STATUS
    assert result["text"] == "Processing..."


def test_DONE_constant_is_valid():
    result = _parse(DONE)
    assert result["type"] == EventType.DONE
