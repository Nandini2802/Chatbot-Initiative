"""
SSE event builders — all SSE output goes through these functions.
Never construct raw SSE strings in endpoint or agent code.

Stream order: html_block → token(s) → followups → done
"""
from __future__ import annotations

import json
from enum import Enum


class EventType(str, Enum):
    HTML_BLOCK = "html_block"
    TOKEN      = "token"
    STATUS     = "status"
    FOLLOWUPS  = "followups"
    ERROR      = "error"
    DONE       = "done"


def _sse(event_type: EventType, payload: dict) -> str:
    data = json.dumps({"type": event_type.value, **payload}, ensure_ascii=False)
    return f"data: {data}\n\n"


def html_block(html: str, card_type: str) -> str:
    return _sse(EventType.HTML_BLOCK, {"html": html, "card_type": card_type})


def token(text: str) -> str:
    return _sse(EventType.TOKEN, {"text": text})


def status(text: str) -> str:
    return _sse(EventType.STATUS, {"text": text})


def followups(chips: list[str]) -> str:
    return _sse(EventType.FOLLOWUPS, {"chips": chips})


def error(message: str, code: str = "INTERNAL_ERROR") -> str:
    return _sse(EventType.ERROR, {"message": message, "code": code})


def done(run_id: str | None = None) -> str:
    payload: dict = {}
    if run_id:
        payload["run_id"] = run_id
    return _sse(EventType.DONE, payload)


DONE = done()
