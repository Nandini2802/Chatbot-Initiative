"""
Unit tests for core/auth/rbac.py
"""
from __future__ import annotations

import pytest

from core.auth import (
    allowed_asset_statuses,
    require_admin,
    require_ingestion_permission,
)
from core.errors import AuthorizationError


def test_broker_allowed_statuses():
    assert allowed_asset_statuses("broker") == ["active"]


def test_rm_allowed_statuses():
    assert "active" in allowed_asset_statuses("rm")
    assert "draft" in allowed_asset_statuses("rm")


def test_admin_allowed_statuses():
    statuses = allowed_asset_statuses("admin")
    assert "active" in statuses
    assert "draft" in statuses
    assert "archived" in statuses


def test_require_admin_passes_for_admin():
    require_admin("admin")  # should not raise


def test_require_admin_fails_for_broker():
    with pytest.raises(AuthorizationError):
        require_admin("broker")


def test_require_ingestion_passes_for_rm():
    require_ingestion_permission("rm")  # should not raise


def test_require_ingestion_fails_for_broker():
    with pytest.raises(AuthorizationError):
        require_ingestion_permission("broker")
