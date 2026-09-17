"""
Shared pytest fixtures for all test layers.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.auth import UserContext


@pytest.fixture
def broker_user() -> UserContext:
    return UserContext(
        broker_id="test-broker-001",
        tenant_id="danube",
        role="broker",
        session_id="test-session-001",
        display_name="Test Broker",
        email="broker@danube.ae",
        org_id="danube-org",
    )


@pytest.fixture
def rm_user() -> UserContext:
    return UserContext(
        broker_id="test-rm-001",
        tenant_id="danube",
        role="rm",
        session_id="test-session-rm",
        display_name="Test RM",
        email="rm@danube.ae",
        org_id="danube-org",
    )


@pytest.fixture
def admin_user() -> UserContext:
    return UserContext(
        broker_id="test-admin-001",
        tenant_id="danube",
        role="admin",
        session_id="test-session-admin",
        display_name="Test Admin",
        email="admin@danube.ae",
        org_id="danube-org",
    )
