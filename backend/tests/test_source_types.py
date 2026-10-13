import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.source_types import evidence_access, peer_reviewed_of, source_type_of
from app.models import Paper

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("journal-article", "journal_article"),
        ("proceedings-article", "proceedings"),
        ("book", "book"),
        ("monograph", "book"),
        ("edited-book", "book"),
        ("reference-book", "book"),
        ("book-chapter", "chapter"),
        ("book-section", "chapter"),
        ("book-part", "chapter"),
        ("posted-content", "preprint"),
        ("dissertation", "thesis"),
        ("report", "other"),
        ("magazine", "other"),
    ],
)
def test_crossref_types(raw, expected):
    assert source_type_of(raw, "10.1/x", crossref=True) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("article", "journal_article"),
        ("review", "journal_article"),
        ("preprint", "preprint"),
        ("conference-paper", "proceedings"),
        ("proceedings-article", "proceedings"),
        ("book", "book"),
        ("book-chapter", "chapter"),
        ("dissertation", "thesis"),
        ("editorial", "other"),
    ],
)
def test_openalex_types(raw, expected):
    assert source_type_of(raw, "10.1/x") == expected


def test_arxiv_doi_is_a_preprint_whatever_the_type_but_a_journal_doi_wins():
    assert source_type_of("article", "10.48550/arXiv.1706.03762") == "preprint"
    assert source_type_of(None, "10.48550/arxiv.1706.03762") == "preprint"
    assert source_type_of("article", "10.5555/journal.1") == "journal_article"


def test_no_type_and_no_arxiv_doi_is_unknown_and_magazine_is_never_inferred():
    assert source_type_of(None, "10.1/x") is None
    assert source_type_of(None, None) is None


def test_peer_review_rule():
    assert [peer_reviewed_of(t) for t in ("preprint", "journal_article", "proceedings", "book", "magazine", None)] == [
        "false",
        "true",
        "true",
        "unknown",
        "unknown",
        "unknown",
    ]


def test_evidence_access():
    assert evidence_access("abs", "ready", True) == "full_text"
    assert evidence_access("abs", "chunking", True) == "abstract"  # still ingesting
    assert evidence_access("abs", "ready", False) == "abstract"
    assert evidence_access(None, "ready", False) == "metadata"


async def test_migration_maps_existing_openalex_types(session):
    spec = importlib.util.spec_from_file_location(
        "m0043", Path(__file__).parents[1] / "alembic/versions/0043_source_types.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    rows = [
        ("article", None),
        ("preprint", None),
        ("conference-paper", None),
        ("review", None),
        ("book-chapter", None),
        (None, "10.48550/arxiv.1234.5678"),
        (None, None),
        ("article", "10.48550/arxiv.9"),
    ]
    papers = [Paper(title=f"mig {i}", file_path="/x", type=t, doi=d) for i, (t, d) in enumerate(rows)]
    session.add_all(papers)
    await session.flush()
    ids = [p.id for p in papers]
    await session.execute(text(migration.BACKFILL_SQL + " AND id = ANY(:ids)"), {"ids": ids})
    await session.commit()
    for p in papers:
        await session.refresh(p)
    assert [(p.source_type, p.peer_reviewed) for p in papers] == [
        ("journal_article", "true"),
        ("preprint", "false"),
        ("proceedings", "true"),
        ("journal_article", "true"),
        ("chapter", "unknown"),
        ("preprint", "false"),
        (None, "unknown"),
        ("preprint", "false"),
    ]
