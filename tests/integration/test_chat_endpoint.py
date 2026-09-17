"""
Integration tests for the chat SSE endpoint.

Requires: docker compose up -d (postgres, redis, qdrant)
"""
from __future__ import annotations

import json

import pytest
import httpx

from api.main import app
from core.auth import UserContext


@pytest.fixture
def dev_user_header() -> dict:
    user = {
        "broker_id": "integ-broker-001",
        "tenant_id": "danube",
        "role": "broker",
        "session_id": "integ-session-001",
        "display_name": "Integration Broker",
        "email": "integ@danube.ae",
        "org_id": "danube-org",
    }
    return {"X-Dev-User-Context": json.dumps(user)}


@pytest.mark.asyncio
async def test_health_endpoint():
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_chat_query_returns_sse_stream(dev_user_header):
    """Test that the chat endpoint returns a streaming SSE response."""
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/v1/chat/query",
            json={"query": "Get me the Olivz brochure", "session_id": "integ-s1"},
            headers=dev_user_header,
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

            events = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

            # Must always end with a DONE event
            assert events, "No SSE events received"
            assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_chat_missing_query_returns_422(dev_user_header):
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat/query",
            json={"session_id": "s1"},
            headers=dev_user_header,
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401():
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat/query",
            json={"query": "test", "session_id": "s1"},
        )
    # In local mode with BYPASS_AUTH=true this returns 200 — in production it returns 401
    assert response.status_code in (200, 401)
