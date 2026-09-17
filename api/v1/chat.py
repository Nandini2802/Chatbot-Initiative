"""
POST /api/v1/chat/query — main SSE streaming endpoint.

Accepts a broker query, runs it through the LangGraph agent,
and streams SSE events: html_block → token(s) → followups → done.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.graph import get_graph
from agent.state import AgentState
from core.auth import UserContext
from core.observability import record_request_latency
from core.sse import done, error, followups, html_block, token

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    session_id: str = Field(..., min_length=1, max_length=128)


def _get_user(request: Request) -> UserContext:
    return request.state.user


async def _stream_events(
    query: str,
    session_id: str,
    user: UserContext,
) -> AsyncGenerator[str, None]:
    start_ms = time.monotonic() * 1000

    print("start_ms", start_ms)

    graph = await get_graph()
    config = {"configurable": {"thread_id": session_id}}

    # Only pass fields that must be reset for this new turn.
    # session_history is intentionally omitted — LangGraph loads it from the
    # checkpoint so the intent node sees the full conversation context.
    initial_state: AgentState = {
        "query": query,
        "user": user,
        "session_id": session_id,
        "tenant_id": user.tenant_id,
        "intent": None,
        "entities": None,
        "confidence": None,
        "path": None,
        "clarification": None,
        "card_html": None,
        "card_type": None,
        "commentary": None,
        "chips": None,
        "genie_conv_id": None,
        "filter_state": None,
    }

    run_id: str | None = None

    try:
        async for event in graph.astream_events(
            initial_state, config=config, version="v2"
        ):
            kind = event["event"]

            if kind == "on_chain_start" and run_id is None:
                run_id = event.get("run_id")

            elif kind == "on_chat_model_stream":
                # Filter to only commentary/retrieval nodes — "intent" and "followups"
                # produce JSON that must NOT be streamed as tokens to the client.
                # Graph node names match what was passed to g.add_node() (no _node suffix).
                node_name = event.get("metadata", {}).get("langgraph_node", "")
                if node_name in {
                    "retrieve_asset",
                    "retrieve_knowledge",
                    "check_version",
                    "clarify",
                    "inventory_query",
                }:
                    chunk = event["data"]["chunk"]
                    if chunk.content and isinstance(chunk.content, str):
                        yield token(chunk.content)

            elif kind == "on_custom_event":
                name = event["name"]
                data = event["data"]

                if name == "html_block":
                    yield html_block(data["html"], data["card_type"])
                elif name == "followups":
                    yield followups(data["chips"])

            elif kind == "on_chain_error":
                logger.error(
                    "agent_stream_error",
                    extra={
                        "session_id": session_id,
                        "error": str(event.get("data", {}).get("error", "")),
                    },
                )
                yield error("Something went wrong. Please try again.", "AGENT_ERROR")

    except Exception as exc:
        logger.error(
            "stream_unhandled_error",
            extra={"session_id": session_id, "error": str(exc)},
        )
        yield error("An unexpected error occurred. Please try again.", "INTERNAL_ERROR")

    finally:
        elapsed_ms = time.monotonic() * 1000 - start_ms
        record_request_latency(elapsed_ms, intent="unknown")
        yield done(run_id=run_id)


@router.post("/query")
async def chat_query(
    body: ChatQueryRequest,
    request: Request,
    user: UserContext = Depends(_get_user),
) -> StreamingResponse:
    print("body", body)
    print("user", user)
    logger.info(
        "chat_query_received",
        extra={
            "session_id": body.session_id,
            "broker_id": user.broker_id,
            "tenant_id": user.tenant_id,
            "query_length": len(body.query),
        },
    )

    return StreamingResponse(
        _stream_events(
            query=body.query,
            session_id=body.session_id,
            user=user,
        ),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
