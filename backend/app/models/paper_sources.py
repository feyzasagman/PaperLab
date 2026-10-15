from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PaperSources(Base):
    """The one row (id true) saying which sources Find papers and Similar ask, their API keys and the contact email."""

    __tablename__ = "paper_sources"

    id: Mapped[bool] = mapped_column(primary_key=True, server_default=text("true"))
    contact_email: Mapped[str | None] = mapped_column(Text)
    openalex_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    crossref_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    semantic_scholar_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    arxiv_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    core_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    unpaywall_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    pubmed_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    pmc_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    europe_pmc_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    zenodo_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    hal_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    acm_dl_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    ssrn_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    doaj_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    openaire_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    iacr_eprint_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    biorxiv_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    medrxiv_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    dblp_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    # Never returned by a route; views carry has_key and key_hint.
    openalex_api_key: Mapped[str | None] = mapped_column(Text)
    semantic_scholar_api_key: Mapped[str | None] = mapped_column(Text)
    core_api_key: Mapped[str | None] = mapped_column(Text)
    pubmed_api_key: Mapped[str | None] = mapped_column(Text)
    pmc_api_key: Mapped[str | None] = mapped_column(Text)
    zenodo_api_key: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
