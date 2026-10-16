import xml.etree.ElementTree as ET

import pytest

from app.core.candidates import Candidate, _combined, from_arxiv, from_crossref, from_s2, from_work, s2_triage
from app.core.workspace_search import _find_or_create_external_ref
from app.providers import arxiv

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

ATOM = """<entry xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>http://arxiv.org/abs/1706.03762v7</id><published>2017-06-12T17:57:34Z</published>
  <title>Attention Is All You Need</title><summary>Sequence models.</summary>
  <author><name>Ashish Vaswani</name></author>{extra}
</entry>"""
PUBLISHED = (
    "<arxiv:doi>10.5555/3295222.3295349</arxiv:doi><arxiv:journal_ref>NIPS 2017\n  pages 5998</arxiv:journal_ref>"
)


def _arxiv(extra: str) -> Candidate:
    return from_arxiv(arxiv._entry(ET.fromstring(ATOM.format(extra=extra))))


def test_arxiv_entry_with_journal_ref_is_published():
    found = _arxiv(PUBLISHED)
    assert (found.source_type, found.published_doi, found.published_venue) == (
        "journal_article", "10.5555/3295222.3295349", "NIPS 2017 pages 5998",
    )  # fmt: skip


def test_arxiv_entry_without_journal_ref_is_a_preprint():
    found = _arxiv("")
    assert (found.source_type, found.published_doi, found.published_venue) == ("preprint", None, None)


def test_openalex_crossref_and_s2_map_their_types():
    work = from_work({"id": "https://openalex.org/W1", "title": "A", "doi": "https://doi.org/10.1/x", "type": "article",
                      "primary_location": {"source": {"display_name": "Nature"}}})  # fmt: skip
    assert (work.source_type, work.published_venue) == ("journal_article", "Nature")
    preprint = from_work({"id": "W2", "title": "B", "doi": "https://doi.org/10.48550/arxiv.1706.1", "type": "article",
                          "primary_location": {"source": {"display_name": "arXiv"}}})  # fmt: skip
    assert (preprint.source_type, preprint.published_venue) == ("preprint", None)
    crossref = from_crossref(
        {"type": "proceedings-article", "DOI": "10.2/Y", "title": ["C"], "container-title": ["ICML"]}
    )
    assert (crossref.source_type, crossref.published_venue) == ("proceedings", "ICML")
    s2 = from_s2({"title": "D", "publicationTypes": ["Conference"], "journal": {"name": "NeurIPS"},
                  "externalIds": {"ArXiv": "1706.03762", "DOI": "10.5555/3295222"}})  # fmt: skip
    assert (s2.source_type, s2.published_doi, s2.published_venue) == ("proceedings", "10.5555/3295222", "NeurIPS")
    assert s2_triage({"externalIds": {"ArXiv": "1706.03762"}, "venue": "arXiv.org"}) == ("preprint", None, None)
    assert s2_triage({"externalIds": {"DOI": "10.1/z"}, "publicationTypes": ["Dataset"]}) == (None, None, None)


def test_combined_keeps_the_first_non_null_triage_value():
    first = Candidate(title="T", source_type="preprint")
    second = Candidate(title="T", source_type="journal_article", published_doi="10.1/a", published_venue="V")
    third = Candidate(title="T", published_doi="10.1/b", published_venue="W")
    merged = _combined([first, second, third])
    assert (merged.source_type, merged.published_doi, merged.published_venue) == ("preprint", "10.1/a", "V")


async def test_existing_ref_triage_is_backfilled_only_when_empty(session):
    first = await _find_or_create_external_ref(
        session, Candidate(title="Triage paper", doi="10.9/triage", source_type="preprint")
    )
    second = await _find_or_create_external_ref(
        session,
        Candidate(title="Triage paper", doi="10.9/triage", source_type="journal_article",
                  published_doi="10.9/pub", published_venue="Journal X"),
    )  # fmt: skip
    third = await _find_or_create_external_ref(
        session, Candidate(title="Triage paper", doi="10.9/triage", published_venue="Other")
    )
    assert first.id == second.id == third.id
    assert (third.source_type, third.published_doi, third.published_venue) == ("preprint", "10.9/pub", "Journal X")
    await session.rollback()
