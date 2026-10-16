import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LLMConnection(Base):
    """A place chat models come from: kind `ollama`, `anthropic` or `openai_compatible`."""

    __tablename__ = "llm_connections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    kind: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, unique=True)
    base_url: Mapped[str | None] = mapped_column(Text)  # null for anthropic, which uses the SDK's address
    api_key: Mapped[str | None] = mapped_column(Text)  # never returned by a route; views carry has_key and key_hint
    # The server answered as a router that forwards to cloud providers (OmniRoute), so a local address isn't local.
    cloud_proxy: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class LLMModel(Base):
    """A model chat lists. At most one has is_default (partial unique index llm_models_one_default)."""

    __tablename__ = "llm_models"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("llm_connections.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
