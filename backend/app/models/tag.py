import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models import workspace_search as _workspace_search  # noqa: F401  (hit_tags points at its hits table)
from app.models.base import Base


class WorkspaceTag(Base):
    __tablename__ = "workspace_tags"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    # Case-insensitive within a workspace; the name is stored as first written.
    __table_args__ = (Index("ix_workspace_tags_name", "workspace_id", func.lower(name), unique=True),)


# Core Tables like workspace_papers. A paper's tag lives and dies with its membership: the composite FK cascades.
paper_tags = Table(
    "paper_tags",
    Base.metadata,
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("paper_id", UUID(as_uuid=True), nullable=False),
    Column("tag_id", UUID(as_uuid=True), ForeignKey("workspace_tags.id", ondelete="CASCADE"), nullable=False),
    PrimaryKeyConstraint("tag_id", "paper_id"),
    ForeignKeyConstraint(
        ["workspace_id", "paper_id"],
        ["workspace_papers.workspace_id", "workspace_papers.paper_id"],
        ondelete="CASCADE",
    ),
    Index("ix_paper_tags_paper", "workspace_id", "paper_id"),
)

hit_tags = Table(
    "hit_tags",
    Base.metadata,
    Column("hit_id", UUID(as_uuid=True), ForeignKey("workspace_search_hits.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", UUID(as_uuid=True), ForeignKey("workspace_tags.id", ondelete="CASCADE"), primary_key=True),
)
