"""
SQLAlchemy ORM models for marketing collateral data.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.session import Base


class MarketingCollateralProject(Base):
    """One row per project from the marketing_collateral API."""

    __tablename__ = "marketing_collateral_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    marketing_collateral_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    project_title: Mapped[str] = mapped_column(String(255), nullable=False)
    project_location: Mapped[str | None] = mapped_column(String(255))
    thumb_full: Mapped[str | None] = mapped_column(Text)
    thumb_large: Mapped[str | None] = mapped_column(Text)
    thumb_med: Mapped[str | None] = mapped_column(Text)
    thumb_thumb: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MarketingCollateralAsset(Base):
    """One row per individual file across all projects and folders."""

    __tablename__ = "marketing_collateral_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    project_title: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    original_name: Mapped[str | None] = mapped_column(String(500))
    relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # relative_path is the stable natural key (project/folder/filename)
        UniqueConstraint("relative_path", name="uq_mc_asset_relative_path"),
    )
