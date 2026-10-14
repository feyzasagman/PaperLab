"""Batch A review fixes (M39 F7): HTML escaping, year suffixes, APA order, names, keys, BibTeX escaping."""

import uuid
from types import SimpleNamespace

import bibtexparser
import pytest
from bibtexparser.middlewares import LatexDecodingMiddleware

from app.core.citations_export import _person, bibtex_keys, csl_item, render


def paper(**kw):
    base = dict(
        id=uuid.uuid4(), title="A study of things", authors=["Jane Smith"], year=2020, venue="Journal of Things",
        doi="10.1234/abc", source_type="journal_article", peer_reviewed="true", evidence_access="full_text",
        published_doi=None, oa_url=None,
    )  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def items_of(*papers):
    return [csl_item(p) for p in papers]


def lines(*papers):
    return render(items_of(*papers), "apa").strip().split("\n")


def test_apa_html_escapes_every_field_but_keeps_our_italics():
    p = paper(title="Deep <script>alert(1)</script> & more", authors=["Eve <b>Mal"], doi="10.1/a_b%c&d<x>",
              venue="J <img>")  # fmt: skip
    html = render(items_of(p), "apa-html")
    assert "<script" not in html and "<b>" not in html and "<img" not in html and "<x>" not in html
    assert "&lt;script&gt;" in html and "10.1/a_b%c&amp;d&lt;x&gt;" in html
    assert "<i>J &lt;img&gt;</i>" in html


def test_same_author_same_year_gets_year_suffixes_in_title_order():
    a, b, c = paper(title="Zed"), paper(title="Alpha"), paper(title="Other", year=2021)
    out = lines(a, b, c)
    assert out[0].startswith("Smith, J. (2020a). Alpha") and out[1].startswith("Smith, J. (2020b). Zed")
    assert out[2].startswith("Smith, J. (2021). Other")


def test_apa_order_authorless_by_title_then_initials_then_single_before_multi():
    anon = paper(authors=[], title="The Pre", doi=None)
    smith_a = paper(authors=["Smith, Amy"], title="X", doi=None)
    smith_j = paper(authors=["Smith, John"], title="X", doi=None)
    smith_j_lee = paper(authors=["Smith, John", "Lee, Ann"], title="A", year=2019, doi=None)
    zed = paper(authors=["Zed Zulu"], title="Z", doi=None)
    out = lines(zed, smith_j_lee, anon, smith_j, smith_a)
    assert out[0].startswith("The Pre")
    assert [line.split(" (")[0] for line in out[1:]] == ["Smith, A.", "Smith, J.", "Smith, J., & Lee, A.", "Zulu, Z."]


def test_authorless_sorts_by_title_among_the_others():
    anon = paper(authors=[], title="An Ordinary Pre", doi=None)
    abel, zulu = paper(authors=["Amy Abel"], title="B", doi=None), paper(authors=["Zed Zulu"], title="Z", doi=None)
    out = lines(zulu, anon, abel)
    assert out[0].startswith("Abel")
    assert out[1].startswith("An Ordinary Pre") and out[2].startswith("Zulu")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Jean-Paul Sartre Jr.", {"family": "Sartre", "given": "Jean-Paul", "suffix": "Jr."}),
        ("Martin Luther King, Jr.", {"family": "King", "given": "Martin Luther", "suffix": "Jr."}),
        ("Henry Ford III", {"family": "Ford", "given": "Henry", "suffix": "III"}),
        ("Smith J", {"family": "Smith", "given": "J"}),
        ("Smith J.A.", {"family": "Smith", "given": "J.A."}),
        ("Smith JA", {"family": "Smith", "given": "JA"}),
        ("Ludwig van Beethoven", {"family": "van Beethoven", "given": "Ludwig"}),
        ("María de la Cruz", {"family": "de la Cruz", "given": "María"}),
        ("de la Cruz, María", {"family": "de la Cruz", "given": "María"}),
    ],
)
def test_person_suffixes_trailing_initials_and_particles(name, expected):
    assert _person(name) == expected


def key_of(title, authors, year=2020):
    p = paper(title=title, authors=authors, year=year)
    item = csl_item(p)
    return bibtex_keys([item])[item["id"]]


@pytest.mark.parametrize(
    "authors,title,key",
    [
        (["Søren Kierkegaard"], "Fear", "kierkegaard2020fear"),
        (["Anna Øvrebø"], "Straße", "ovrebo2020strasse"),
        (["Åsa Łukasz"], "Œuvre", "lukasz2020oeuvre"),
        (["Jan Đorđević"], "Þing", "dordevic2020thing"),
        (["Zoë Ægir"], "Cat", "aegir2020cat"),
    ],
)
def test_keys_transliterate_letters_nfkd_cannot_decompose(authors, title, key):
    assert key_of(title, authors) == key


def test_keys_fall_back_when_a_part_folds_to_nothing():
    assert key_of("深度学习", ["李明"]) == "anon2020untitled"
    assert key_of("深度学习", ["Jane Smith"]) == "smith2020untitled"


def test_bibtex_title_with_braces_and_specials_round_trips_after_unescaping():
    title = r"Deep {DNA} & 50% of $x$ #1 a_b ~ \ end"
    text = render(items_of(paper(title=title)), "bibtex")
    entry = bibtexparser.parse_string(text, parse_stack=[LatexDecodingMiddleware()]).entries[0]
    assert entry["title"] == title


def test_a_preprint_note_does_not_repeat_the_genre():
    p = paper(source_type="preprint", peer_reviewed="false", doi="10.48550/arXiv.2401.00001", venue=None)
    assert csl_item(p)["note"] == "Not peer-reviewed · full text in PaperLab"
    assert render(items_of(p), "bibtex").count("Preprint") == 1


def test_bibtex_caret_is_escaped():
    assert r"a \textasciicircum{} b" in render(items_of(paper(title="a ^ b")), "bibtex")


def test_apa_order_folds_diacritics_like_the_keys():
    out = lines(paper(authors=["Å. Øberg"], title="One"), paper(authors=["J. Smith"], title="Two"),
                paper(authors=["Zoë Zed"], title="Three"))  # fmt: skip
    assert [ln.split(",")[0] for ln in out] == ["Øberg", "Smith", "Zed"]


def test_apa_order_ignores_leading_punctuation_and_articles_in_titles():
    out = lines(paper(title="<script>alert(1)</script> zebra"), paper(title="Another study"))
    assert "Another study" in out[0] and "zebra" in out[1]
    assert "2020a" in out[0] and "2020b" in out[1]


def test_apa_html_escapes_a_hostile_title():
    html = render(items_of(paper(title="<img onerror=x>")), "apa-html")
    assert "&lt;img" in html and "<img" not in html
