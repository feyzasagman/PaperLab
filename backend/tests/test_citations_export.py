import uuid
from types import SimpleNamespace

import pytest

from app.core.citations_export import bibtex_keys, csl_item


def paper(**kw):
    base = dict(
        id=uuid.uuid4(),
        title="A study of things",
        authors=["Jane Smith"],
        year=2020,
        venue="Journal of Things",
        doi="10.1234/abc",
        source_type="journal_article",
        peer_reviewed="true",
        evidence_access="full_text",
        published_doi=None,
        oa_url=None,
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
    assert "Not peer-reviewed" in item["note"]
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


# ---- Task 2: renderers ----

import json  # noqa: E402

import bibtexparser  # noqa: E402
import rispy  # noqa: E402

from app.core.citations_export import FORMATS, render  # noqa: E402


def items_of(*papers):
    return [csl_item(p) for p in papers]


def test_formats_table_and_unknown_format():
    assert set(FORMATS) == {"bibtex", "ris", "csljson", "apa", "apa-html"}
    assert FORMATS["bibtex"][1] == "bib" and FORMATS["ris"][1] == "ris" and FORMATS["csljson"][1] == "json"
    with pytest.raises(ValueError, match="unknown_format"):
        render([], "docx")


def test_csljson_is_the_items_as_a_json_array():
    items = items_of(paper(), paper(title="Other"))
    assert json.loads(render(items, "csljson")) == items


def test_bibtex_round_trips_and_keeps_unicode():
    a = paper(authors=["Hans Müller", "World Health Organization"], title="R&D of light")
    b = paper(source_type="preprint", doi="10.48550/arXiv.2401.00001", venue=None, authors=["Rosa Ñúñez"], title="Pre")
    c = paper(source_type="proceedings", venue="Proc. of Things", authors=["Al Smith"], title="Conf paper", doi=None,
              oa_url="https://example.org/a_b.pdf")  # fmt: skip
    text = render(items_of(a, b, c), "bibtex")
    assert "Müller" in text and "Ñúñez" in text and r"R\&D" in text
    lib = bibtexparser.parse_string(text)
    by_title = {e["title"]: e for e in lib.entries}
    assert [e.entry_type for e in lib.entries] == ["article", "misc", "inproceedings"]
    assert {e.key for e in lib.entries} == {"muller2020rd", "nunez2020pre", "smith2020conf"}
    first = by_title[r"R\&D of light"]
    assert first["author"] == "Müller, Hans and {World Health Organization}"
    assert first["doi"] == "10.1234/abc" and first["journal"] == "Journal of Things" and first["year"] == "2020"
    assert by_title["Pre"]["doi"] == "10.48550/arXiv.2401.00001" and "Preprint" in by_title["Pre"]["note"]
    assert by_title["Conf paper"]["url"] == "https://example.org/a_b.pdf"
    assert by_title["Conf paper"]["booktitle"] == "Proc. of Things"


def test_ris_round_trips_and_keeps_unicode():
    a = paper(authors=["Hans Müller", "World Health Organization"])
    b = paper(source_type="chapter", authors=["Al Smith"], title="Chap", editors=["Ann Lee"], venue="Big Book")
    entries = rispy.loads(render(items_of(a, b), "ris"))
    assert [e["type_of_reference"] for e in entries] == ["JOUR", "CHAP"]
    assert entries[0]["title"] == "A study of things" and entries[0]["doi"] == "10.1234/abc"
    assert entries[0]["authors"] == ["Müller, Hans", "World Health Organization"]
    assert entries[0]["secondary_title"] == "Journal of Things" and entries[0]["year"] == "2020"
    assert entries[0]["notes"] == ["Source: journal article · peer-reviewed · full text in PaperLab"]
    assert entries[1]["secondary_authors"] == ["Lee, Ann"]


def test_apa_plain_text_for_an_article_and_html_italics():
    items = items_of(paper(authors=["Jane Smith"], title="A study of things"))
    text = render(items, "apa")
    assert text.startswith("Smith, J. (2020). A study of things. Journal of Things")
    assert text.rstrip().endswith("https://doi.org/10.1234/abc")
    assert "<i>Journal of Things</i>" in render(items, "apa-html")
    assert "<" not in text


def test_apa_lists_nineteen_authors_then_an_ellipsis_and_the_last():
    names = [f"Given{i} Family{i:02d}" for i in range(23)]
    text = render(items_of(paper(authors=names)), "apa")
    assert "Family18" in text and "Family19" not in text and "Family21" not in text
    assert ". . . Family22" in text or "… Family22" in text


def test_apa_organisation_diacritics_and_no_doi_url():
    text = render(
        items_of(paper(authors=["World Health Organization"], doi=None, oa_url="https://example.org/x")), "apa"
    )
    assert text.startswith("World Health Organization. (2020)") and text.rstrip().endswith("https://example.org/x")
    assert "Müller, H." in render(items_of(paper(authors=["Hans Müller"])), "apa")


def test_apa_conference_paper_and_chapter_with_editors():
    conf = paper(source_type="proceedings", venue="Proceedings of Things", title="Conf talk")
    assert "Proceedings of Things" in render(items_of(conf), "apa")
    chap = paper(source_type="chapter", venue="Big Book", title="Chap", editors=["Ann Lee"])
    # Known deviation (D254): citeproc-py renders the editor term as "(ed.)", APA 7 wants "(Ed.)". Not patched by hand.
    text = render(items_of(chap), "apa")
    assert "In A. Lee (" in text and "Big Book" in text


def test_apa_preprint_names_the_server():
    text = render(items_of(paper(source_type="preprint", doi="10.48550/arXiv.2401.00001", venue=None)), "apa")
    assert "arXiv" in text and "https://doi.org/10.48550/arXiv.2401.00001" in text


def test_apa_is_sorted_by_author_and_one_paragraph_per_item():
    a, b = paper(authors=["Zed Zulu"], title="Zeta"), paper(authors=["Amy Abel"], title="Alpha")
    lines = render(items_of(a, b), "apa").strip().split("\n")
    assert [line.split(",")[0] for line in lines] == ["Abel", "Zulu"]
