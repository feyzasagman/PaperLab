import json

import pytest

from app.core import citations, claims, workspaces
from app.core.errors import InvalidInput
from app.models import Paper, PaperStatus

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

ABSTRACT = "Outputs were 23% less diverse."


class Scripted:
    model, connection_name = "fake:scripted", "Test"

    async def stream(self, system, prompt):
        yield json.dumps({"verdict": "not_addressed", "passage": None, "quote": "", "reason": "Nothing on it."})


async def add(session, title, authors, year, doi=None):
    paper = Paper(
        title=title, file_path="x.pdf", status=PaperStatus.READY, authors=authors, year=year, doi=doi, abstract=ABSTRACT
    )
    session.add(paper)
    await session.flush()
    return paper


def test_sentences_keep_abbreviations_intact():
    text = "Smith et al. (2021) found it, e.g. in Fig. 2. A second claim (Lee, 2020). Third? Yes."
    assert citations.split_sentences(text) == [
        "Smith et al. (2021) found it, e.g. in Fig. 2.",
        "A second claim (Lee, 2020).",
        "Third?",
        "Yes.",
    ]


def test_a_doi_is_found_without_its_trailing_punctuation():
    [found] = citations.find("As shown (https://doi.org/10.1234/abc.5).")
    assert (found.kind, found.doi) == ("doi", "10.1234/abc.5")


@pytest.mark.parametrize(
    "sentence",
    [
        "It holds (Smith, 2021).",
        "It holds (Smith et al., 2021).",
        "Smith (2021) says it holds.",
        "Smith et al. (2021) say it holds.",
        "It holds (Smith & Lee, 2021).",
        "It holds (see Smith and Lee, 2021a).",
    ],
)
def test_author_year_forms_give_the_first_author_and_year(sentence):
    [found] = citations.find(sentence)
    assert (found.kind, found.surname, found.year) == ("author_year", "Smith", 2021)


def test_two_citations_in_one_parenthesis_are_two():
    smith, lee = citations.find("It holds (Smith, 2021; Lee, 2020).")
    assert (smith.surname, smith.year, lee.surname, lee.year) == ("Smith", 2021, "Lee", 2020)


def test_prose_without_a_citation_finds_nothing():
    assert citations.find("In 2021 the models grew (a lot).") == []


async def test_surname_matching_ignores_case_and_accents(session):
    paper = await add(session, "Fine paper", ["Anna Müller", "B. Other"], 1987)
    for spelling in ("Muller", "MÜLLER", "müller"):
        found = await citations.match(
            session, citations.Citation("author_year", "", (0, 0), surname=spelling, year=1987)
        )
        assert found.paper_ids == [paper.id]


async def test_only_the_first_author_and_the_year_count(session):
    await add(session, "Second author", ["A. First", "Zeta Qwerty"], 1987)
    await add(session, "Wrong year", ["Zeta Qwerty"], 1986)
    right = await add(session, "Particle", ["Jan van der Qwerty"], 1987)
    cite = citations.Citation("author_year", "", (0, 0), surname="van der Qwerty", year=1987)
    assert (await citations.match(session, cite)).paper_ids == [right.id]
    assert (
        await citations.match(session, citations.Citation("author_year", "", (0, 0), surname="Qwerty", year=1987))
    ).paper_ids == [right.id]


async def test_a_doi_matches_case_insensitively(session):
    paper = await add(session, "Has a DOI", ["A. Author"], 1987, doi="10.9999/AbC")
    found = await citations.match(session, citations.Citation("doi", "", (0, 0), doi="10.9999/abc"))
    assert (found.paper_ids, found.titles) == ([paper.id], ["Has a DOI"])


async def test_matching_can_be_limited_to_a_workspace(session):
    inside = await add(session, "Inside", ["A. Workspaced"], 1987)
    await add(session, "Outside", ["B. Workspaced"], 1987)
    space = await workspaces.create(session, "Citation scope test")
    await workspaces.add_paper(session, space.id, inside.id)
    cite = citations.Citation("author_year", "", (0, 0), surname="Workspaced", year=1987)
    assert len((await citations.match(session, cite)).paper_ids) == 2
    assert (await citations.match(session, cite, space.id)).paper_ids == [inside.id]


async def test_verify_claims_checks_in_order_with_the_citation_removed(session):
    first = await add(session, "First paper", ["Ann Ordering"], 1987)
    second = await add(session, "Second paper", ["Bo Ordering2"], 1988)
    text = "Models flatten style (Ordering, 1987). Readers notice Ordering2 (1988) said so. No citation here."
    items = await claims.verify_claims(session, text, llm=Scripted())
    assert [i.claim for i in items] == ["Models flatten style.", "Readers notice said so."]
    assert [i.check.paper_id for i in items] == [first.id, second.id]
    assert all(i.error is None for i in items)


async def test_no_match_is_citation_not_found_and_a_missing_doi_makes_no_model_call(session):
    items = await claims.verify_claims(session, "A claim (Nobodyxyz, 1987). Another (10.9999/missing-one).")
    assert [i.error for i in items] == ["citation_not_found", "citation_not_found"]
    assert all(i.check is None for i in items)


async def test_two_matches_are_ambiguous_with_their_titles_and_never_checked(session):
    await add(session, "Twin A", ["Ann Twinsurname"], 1987)
    await add(session, "Twin B", ["Bob Twinsurname"], 1987)
    [item] = await claims.verify_claims(session, "It holds (Twinsurname, 1987).", llm=Scripted())
    assert item.error == "ambiguous_citation" and item.check is None
    assert sorted(item.candidates) == ["Twin A", "Twin B"]


async def test_more_than_thirty_cited_claims_is_refused(session):
    text = " ".join(f"Claim {i} (10.1000/x{i})." for i in range(31))
    with pytest.raises(InvalidInput) as raised:
        await claims.verify_claims(session, text)
    assert str(raised.value.args[0]) == "too_many_claims"
