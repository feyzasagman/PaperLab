import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SavedSearch(Base):
    """A workspace search that runs on a schedule (M39 F2). `schedule` is a 5-field cron string read in `timezone`."""

    __tablename__ = "saved_searches"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))  # [] = every one turned on
    year_from: Mapped[int | None] = mapped_column(Integer, default=None)
    year_to: Mapped[int | None] = mapped_column(Integer, default=None)
    schedule: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    next_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
