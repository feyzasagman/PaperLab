import uuid
from types import SimpleNamespace

import pytest

from app.core.citations_export import bibtex_keys, csl_item


def paper(**kw):
    base = dict(
        id=uuid.uuid4(), title="A study of things", authors=["Jane Smith"], year=2020, venue="Journal of Things",
        doi="10.1234/abc", source_type="journal_article", peer_reviewed="true", evidence_access="full_text",
        published_doi=None, oa_url=None,
    )
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize(
    "source_type,csl",
    [
        ("journal_article", "article-journal"),
        ("proceedings", "paper-conference"),
        ("book", "book"),
        ("chapter", "chapter"),
        ("preprint", "article"),
        ("magazine", "article-magazine"),
        ("thesis", "thesis"),
        ("other", "document"),
        (None, "article-journal"),
    ],
)
def test_source_type_maps_to_csl_type(source_type, csl):
    assert csl_item(paper(source_type=source_type))["type"] == csl


def test_a_journal_article_carries_the_basics():
    item = csl_item(paper())
    assert item["title"] == "A study of things"
    assert item["author"] == [{"family": "Smith", "given": "Jane"}]
    assert item["issued"] == {"date-parts": [[2020]]}
    assert item["container-title"] == "Journal of Things"
    assert item["DOI"] == "10.1234/abc"
    assert "URL" not in item
    assert item["note"] == "Source: journal article · peer-reviewed · full text in PaperLab"


def test_a_preprint_has_genre_and_the_server_as_publisher():
    item = csl_item(paper(source_type="preprint", peer_reviewed="false", doi="10.48550/arXiv.2401.00001", venue=None))
    assert (item["type"], item["genre"], item["publisher"]) == ("article", "Preprint", "arXiv")
    assert "container-title" not in item
    assert "not peer-reviewed" in item["note"]
    assert csl_item(paper(source_type="preprint", doi=None, venue="bioRxiv"))["publisher"] == "bioRxiv"


def test_authors_split_or_stay_literal():
    names = ["Smith, Jane", "Ludwig van Beethoven", "World Health Organization", "UNESCO", "Plato"]
    out = csl_item(paper(authors=names))["author"]
    assert out == [
        {"family": "Smith", "given": "Jane"},
        {"family": "van Beethoven", "given": "Ludwig"},
        {"literal": "World Health Organization"},
        {"literal": "UNESCO"},
        {"literal": "Plato"},
    ]


def test_url_only_without_a_doi():
    item = csl_item(paper(doi=None, oa_url="https://example.org/p.pdf"))
    assert "DOI" not in item and item["URL"] == "https://example.org/p.pdf"
    assert "URL" not in csl_item(paper(doi=None, oa_url=None))


def test_missing_year_venue_and_unknown_peer_review_are_left_out():
    item = csl_item(paper(year=None, venue=None, peer_reviewed="unknown", evidence_access="abstract"))
    assert "issued" not in item and "container-title" not in item
    assert item["note"] == "Source: journal article · abstract only in PaperLab"


def test_editors_and_volume_are_used_when_the_paper_has_them():
    item = csl_item(paper(source_type="chapter", editors=["Ann Lee"], volume="3", issue="2", pages="10-20"))
    assert item["editor"] == [{"family": "Lee", "given": "Ann"}]
    assert (item["volume"], item["issue"], item["page"]) == ("3", "2", "10-20")


def keys(*papers):
    items = [{**csl_item(p), "id": str(p.id)} for p in papers]
    return bibtex_keys(items), items


def test_keys_drop_diacritics_and_skip_stop_words():
    a = paper(authors=["Hans Müller"], title="The Effect of Light")
    b = paper(authors=["Rosa Ñúñez"], title="On a Theory", year=None)
    c = paper(authors=["World Health Organization"], title="Report 2020")
    k, _ = keys(a, b, c)
    assert [k[str(p.id)] for p in (a, b, c)] == ["muller2020effect", "nunezndtheory", "world2020report"]


def test_colliding_keys_get_stable_suffixes_by_title_then_id():
    a = paper(authors=["Al Smith"], title="The effect of heat")
    b = paper(authors=["Al Smith"], title="Effect of cold")
    c = paper(authors=["Al Smith"], title="Other thing")
    k1, _ = keys(a, b, c)
    k2, _ = keys(c, b, a)
    assert k1 == k2
    assert k1[str(b.id)] == "smith2020effecta" and k1[str(a.id)] == "smith2020effectb"
    assert k1[str(c.id)] == "smith2020other"
