"""Marketing collateral tables

Revision ID: 001_marketing_collateral
Revises: 
Create Date: 2026-06-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "001_marketing_collateral"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketing_collateral_projects",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("marketing_collateral_id", sa.Integer, nullable=False),
        sa.Column("project_id", sa.Integer, nullable=False),
        sa.Column("project_title", sa.String(255), nullable=False),
        sa.Column("project_location", sa.String(255)),
        sa.Column("thumb_full", sa.Text),
        sa.Column("thumb_large", sa.Text),
        sa.Column("thumb_med", sa.Text),
        sa.Column("thumb_thumb", sa.Text),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mc_projects_marketing_collateral_id", "marketing_collateral_projects", ["marketing_collateral_id"], unique=True)
    op.create_index("ix_mc_projects_project_id", "marketing_collateral_projects", ["project_id"])

    op.create_table(
        "marketing_collateral_assets",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer, nullable=False),
        sa.Column("project_title", sa.String(255), nullable=False),
        sa.Column("folder_key", sa.String(100), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("original_name", sa.String(500)),
        sa.Column("relative_path", sa.String(1000), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("mime_type", sa.String(100)),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("uploaded_at", sa.DateTime(timezone=False)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("relative_path", name="uq_mc_asset_relative_path"),
    )
    op.create_index("ix_mc_assets_project_id", "marketing_collateral_assets", ["project_id"])
    op.create_index("ix_mc_assets_folder_key", "marketing_collateral_assets", ["folder_key"])


def downgrade() -> None:
    op.drop_table("marketing_collateral_assets")
    op.drop_table("marketing_collateral_projects")
