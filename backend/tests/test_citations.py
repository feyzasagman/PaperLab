import json
from pathlib import Path

import pytest

from app.core import citations, claims, workspaces
from app.core.claim_text import split_references
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
    assert [i.claim for i in items] == ["Models flatten style.", "Readers notice Ordering2 said so."]
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


def test_a_sentence_opening_word_before_a_year_is_not_a_surname():
    assert citations.find("In (2021) the models grew.") == []
    assert citations.find("Since 2021 they grew. After (2020) too.") == []


def test_a_bracketed_abbreviation_does_not_split_a_sentence():
    assert citations.split_sentences("Results (p. 4). Next one.") == ["Results (p. 4).", "Next one."]
    assert citations.split_sentences("Results [p. 4] Next.") == ["Results [p. 4] Next."]


async def test_one_failing_check_does_not_lose_the_others(session):
    from app.providers.base import LLMError

    class Flaky(Scripted):
        calls = 0

        async def stream(self, system, prompt):
            Flaky.calls += 1
            if Flaky.calls == 2:
                raise LLMError("boom")
            async for part in super().stream(system, prompt):
                yield part

    for n, name in enumerate(("Aaflaky", "Bbflaky", "Ccflaky")):
        await add(session, f"Flaky {n}", [f"X {name}"], 1987)
    text = "One claim here (Aaflaky, 1987). Two claim here (Bbflaky, 1987). Three claim here (Ccflaky, 1987)."
    items = await claims.verify_claims(session, text, llm=Flaky())
    assert [bool(i.check) for i in items] == [True, False, True]
    assert items[1].error == "check_failed"


@pytest.mark.parametrize("authors", [["Smith, J."], ["Smith J"], ["Smith J.K."], ["John Smith"]])
async def test_byline_forms_all_match_the_surname(session, authors):
    assert citations._first_author_is(authors, "Smith")


def test_a_different_surname_with_the_same_start_does_not_match():
    assert not citations._first_author_is(["Smithson, A."], "Smith")
    assert not citations._first_author_is(["Smith Jonathan Q"], "Smith")


def test_see_also_before_a_citation_is_stripped():
    found = citations.find("It holds (Smith et al., 2021; see also Lee, 2020).")
    assert [(c.surname, c.year) for c in found] == [("Smith", 2021), ("Lee", 2020)]


@pytest.mark.parametrize(
    ("sentence", "doi"),
    [
        ("It holds (doi:10.1016/0140-6736(93)00000-X).", "10.1016/0140-6736(93)00000-x"),
        ("See https://doi.org/10.1000/ABC.", "10.1000/abc"),
    ],
)
def test_dois_are_normalised_with_balanced_brackets_kept(sentence, doi):
    [found] = citations.find(sentence)
    assert found.doi == doi
    assert ")" not in sentence[: found.span[0]] and sentence[found.span[1] :].strip() in {").", "."}


def test_an_initial_before_a_narrative_citation_does_not_split_the_sentence():
    sentence = "As noted by J. Smith (2021), models flatten style."
    assert citations.split_sentences(sentence) == [sentence]
    assert [(c.surname, c.year) for c in citations.find(sentence)] == [("Smith", 2021)]


async def test_overlapping_citation_spans_keep_the_rest_of_the_claim(session):
    await add(session, "Overlap paper", ["Ann Overlapsurname"], 2021, doi="10.1000/overlap")
    text = "It holds (Overlapsurname, 2021; doi:10.1000/overlap) in all adult cases."
    items = await claims.verify_claims(session, text, llm=Scripted())
    assert {i.claim for i in items} == {"It holds in all adult cases."}


async def test_a_claim_with_under_three_words_is_empty_and_asks_no_model(session):
    await add(session, "Short paper", ["Ann Shortsurname"], 1987)

    class Never(Scripted):
        async def stream(self, system, prompt):
            raise AssertionError("no model call")
            yield

    [item] = await claims.verify_claims(session, "Yes (Shortsurname, 1987).", llm=Never())
    assert item.error == "empty_claim" and item.check is None


@pytest.mark.parametrize(
    "sentence, surname, year, text",
    [
        ("Anderson, Shah & Kreminski (2024) found it.", "Anderson", 2024, "Anderson, Shah & Kreminski (2024)"),
        ("Anderson, Shah, and Kreminski (2024) found it.", "Anderson", 2024, "Anderson, Shah, and Kreminski (2024)"),
        ("It holds (Anderson, Shah, & Kreminski, 2024).", "Anderson", 2024, "Anderson, Shah, & Kreminski, 2024"),
        ("Anderson et al. (2024) found it.", "Anderson", 2024, "Anderson et al. (2024)"),
        ("Doshi & Hauser (2024) found it.", "Doshi", 2024, "Doshi & Hauser (2024)"),
        ("Runco and Jaeger (2012) found it.", "Runco", 2012, "Runco and Jaeger (2012)"),
        ("Plucker et al. (2004, p. 90) define it.", "Plucker", 2004, "Plucker et al. (2004, p. 90)"),
        (
            "Ismayilzada, Paul, Bosselut & van der Plas (2024) survey it.",
            "Ismayilzada",
            2024,
            "Ismayilzada, Paul, Bosselut & van der Plas (2024)",
        ),
        ("However, Smith and Lee (2020) argue it.", "Smith", 2020, "Smith and Lee (2020)"),
        ("Thus, Smith, Jones and Lee (2021) agree.", "Smith", 2021, "Smith, Jones and Lee (2021)"),
    ],
)
def test_name_lists_keep_the_first_author(sentence, surname, year, text):
    [found] = citations.find(sentence)
    assert (found.surname, found.year, found.text) == (surname, year, text)
    assert found.narrative == (not sentence.startswith("It holds"))  # only "(Anderson, Shah, & Kreminski, 2024)" isn't


FIXTURES = Path(__file__).parent / "fixtures" / "claims"


def pilot(name: str) -> list:
    return claims.cited_claims(split_references((FIXTURES / name).read_text())[0])


def by(found, surname, year):
    return [c for c in found if (c.citation.surname, c.citation.year) == (surname, year)]


def test_boden_2009_is_judged_on_his_own_sentence_not_abrahams():
    found = pilot("definition.md")
    assert len(found) == 20
    [boden] = by(found, "Boden", 2009)
    assert boden.claim == (
        'Boden notes that "valuable" can mean interesting, useful, beautiful, simple, richly complex and more.'
    )
    abraham = [c.claim for c in by(found, "Abraham", 2025)]
    assert 'Abraham proposes that a creative idea is one that is "both novel and satisfying".' in abraham


def test_no_pilot_claim_holds_markdown():
    for name in ("definition.md", "ai-creativity-benchmarks.md"):
        for cited in pilot(name):
            assert not any(mark in cited.claim for mark in ("|", "##", "**", "[[", "]]")), cited.claim
            assert not cited.claim.startswith(("#", "- ")), cited.claim


def test_a_table_row_citation_is_judged_on_the_rows_other_cells():
    found = pilot("ai-creativity-benchmarks.md")
    assert len(found) == 46
    row = [c for c in by(found, "Anderson", 2024) if c.error is None][0]
    assert row.claim == (
        "C&C '24. 36-participant ideation study, ChatGPT vs an alternative creativity support tool. "
        "Different users produced less semantically distinct ideas with ChatGPT."
    )


def test_a_cross_reference_is_not_a_claim():
    found = pilot("ai-creativity-benchmarks.md")
    first_four = found[:4]
    assert [(c.citation.surname, c.error) for c in first_four] == [
        ("Koivisto", "not_a_claim"),
        ("Hubert", "not_a_claim"),
        ("Doshi", "not_a_claim"),
        ("Anderson", "not_a_claim"),
    ]
    assert first_four[0].claim.endswith("Hubert et al. and Doshi & Hauser are in computational creativity.")
    assert found[-1].error == "not_a_claim"  # "- [[llm-homogenization]]: the full treatment of Anderson, …"


def test_a_label_citation_takes_the_rest_of_its_bullet():
    [gomez] = by(pilot("ai-creativity-benchmarks.md"), "Gómez-Rodríguez", 2023)[:1]
    assert gomez.claim.startswith("The prompt is deliberately unusual: Ignatius J. Reilly from A Confederacy of Dunces")


def test_three_citations_in_one_sentence_share_that_sentence_only():
    found = claims.cited_claims(
        "Earlier text (Zed, 2001). Several studies found it (Ann, 2020; Bo & Cy, 2021; Di et al., 2022). Later text."
    )
    assert [(c.citation.surname, c.claim) for c in found[1:]] == [
        ("Ann", "Several studies found it."),
        ("Bo", "Several studies found it."),
        ("Di", "Several studies found it."),
    ]


def test_a_bare_label_bullet_is_still_an_empty_claim():
    [cited] = claims.cited_claims("- **Smith (2020).**")
    assert cited.claim == "" and cited.error is None  # verify_claims turns it into empty_claim without a model call


async def test_verify_claims_reports_a_cross_reference_without_matching_it(session):
    class Never(Scripted):
        async def stream(self, system, prompt):
            raise AssertionError("no model call")
            yield

    [item] = await claims.verify_claims(session, "Smith & Lee (1987) is covered in [[some-note]].", llm=Never())
    assert (item.error, item.check) == ("not_a_claim", None)


def test_a_label_that_is_not_first_in_its_block_gets_no_claim_from_other_sentences():
    found = claims.cited_claims(
        "Other finding about novelty was large here. Smith (2020) agrees. More about other people (Lee, 2019)."
    )
    assert [(c.citation.surname, c.claim) for c in found] == [
        ("Smith", "Smith agrees."),  # two words: verify_claims reports empty_claim
        ("Lee", "More about other people."),
    ]


def test_a_first_sentence_label_stops_before_the_next_cited_sentence():
    found = claims.cited_claims("- **Smith (2020).** Finding one holds. Finding two holds (Lee, 2019).")
    assert [(c.citation.surname, c.claim) for c in found] == [
        ("Smith", "Finding one holds."),
        ("Lee", "Finding two holds."),
    ]


def test_a_table_rows_claim_leaves_out_other_cells_that_cite_papers():
    found = claims.cited_claims("| A | B | C |\n|---|---|---|\n| Smith (2020) | Lee (2019) | Finding holds broadly |")
    assert [(c.citation.surname, c.claim) for c in found] == [("Smith", ""), ("Lee", "")]  # two cited cells


def test_a_citation_without_a_final_period_stays_inside_its_block():
    found = claims.cited_claims("- Point has no final period (Lee, 2021)\n\n## Heading\n\nNext text.")
    assert [c.claim for c in found] == ["Point has no final period."]


def test_a_near_miss_cross_reference_is_still_a_claim():
    texts = ("Effects are in line with [[theory]] (Lee, 2021).", "Scores are higher in [[x]]-style tasks (Lee, 2021).")
    for text in texts:
        [cited] = claims.cited_claims(text)
        assert cited.error is None, text


@pytest.mark.parametrize("written", ["Gómez-Rodríguez", "Gomez-Rodriguez", "Gómez–Rodríguez", "GÓMEZ-RODRÍGUEZ"])
def test_any_dash_and_accent_folds(written):
    assert citations._first_author_is(["Carlos Gómez‐Rodríguez"], written)


async def test_a_year_off_by_one_matches_the_only_candidate_and_says_so(session):
    paper = await add(session, "Offbyone paper", ["Anil R. Offbyone"], 1987, doi="10.1000/m46-offbyone")
    cite = citations.Citation("author_year", "", (0, 0), surname="Offbyone", year=1988)
    found = await citations.match(session, cite)
    assert (found.paper_ids, found.library_year) == ([paper.id], 1987)
    [item] = await claims.verify_claims(
        session, "Individual creativity rose a lot (Offbyone & Hauser, 1988).", llm=Scripted()
    )
    assert (item.error, item.warning, item.library_year, item.check.paper_id) == (None, "year_mismatch", 1987, paper.id)


async def test_the_cited_year_wins_over_a_neighbouring_one(session):
    exact = await add(session, "Exact year", ["Ann Yearwinner"], 1988)
    await add(session, "Year before", ["Ann Yearwinner"], 1987)
    cite = citations.Citation("author_year", "", (0, 0), surname="Yearwinner", year=1988)
    found = await citations.match(session, cite)
    assert (found.paper_ids, found.library_year) == ([exact.id], None)


async def test_two_neighbouring_years_are_ambiguous(session):
    await add(session, "Before", ["Ann Twoyears"], 1987)
    await add(session, "After", ["Ann Twoyears"], 1989)
    [item] = await claims.verify_claims(session, "Something holds broadly (Twoyears, 1988).", llm=Scripted())
    assert item.error == "ambiguous_citation" and sorted(item.candidates) == ["After", "Before"]


async def test_the_reference_list_doi_beats_duplicates_and_missing_metadata(session):
    with_doi = await add(session, "Homogenization (DOI only)", [], None, doi="10.1000/m46-homog")
    await add(session, "Homogenization copy A", ["Barrett R. Refdoi"], 1987)
    await add(session, "Homogenization copy B", ["Barrett R. Refdoi"], 1987)
    body = "Different users produced less distinct ideas (Refdoi, Shah & Kreminski, 1987)."
    refs = (
        "\n\n## References\n\n"
        "- Refdoi, B. R., Shah, J. H., & Kreminski, M. (1987). H. https://doi.org/10.1000/m46-homog\n"
    )
    [item] = await claims.verify_claims(session, body + refs, llm=Scripted())
    assert (item.error, item.check.paper_id) == (None, with_doi.id)
    [item] = await claims.verify_claims(session, body, llm=Scripted())
    assert item.error == "ambiguous_citation"


async def test_a_reference_doi_missing_from_the_library_falls_back_to_author_year(session):
    paper = await add(session, "Fallback paper", ["Ann Fallbackname"], 1987)
    note = (
        "It holds in general (Fallbackname, 1987).\n\n## References\n\n"
        "- Fallbackname, A. (1987). F. https://doi.org/10.1000/not-here\n"
    )
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert item.check.paper_id == paper.id


def test_a_short_first_sentence_without_a_bold_lead_is_not_a_label():
    [smith] = claims.cited_claims("Smith (2020) found convergence. Other work disagrees strongly with this view.")
    assert smith.claim == "Smith found convergence."
    [smith] = claims.cited_claims("Creativity rose (Smith, 2020). Diversity fell in every group studied.")
    assert smith.claim == "Creativity rose."  # two words: verify_claims reports empty_claim
    [smith] = claims.cited_claims("- Smith (2020). Finding one holds.")
    assert smith.claim == ""
    [smith] = claims.cited_claims("- __Smith (2020).__ Finding one holds.")
    assert smith.claim == "Finding one holds."


def test_a_table_citation_is_judged_on_its_own_cell_first():
    note = (
        "| Topic | Finding | Type |\n|---|---|---|\n"
        "| Homogenization | Ideas converge across users (Smith, 2020) | Lab study |"
    )
    [smith] = claims.cited_claims(note)
    assert smith.claim.startswith("Ideas converge across users.") and "Lab study" in smith.claim


def test_a_cited_cell_never_borrows_another_cited_cells_finding():
    found = claims.cited_claims("| A | B | C |\n|---|---|---|\n| Smith (2020) | Fluency rose | Lee (2021) |")
    assert [(c.citation.surname, c.claim) for c in found] == [("Smith", ""), ("Lee", "")]
    found = claims.cited_claims(
        "| A | B |\n|---|---|\n| Ideas converge widely (Smith, 2020) | Fluency rose fast (Lee, 2021) |"
    )
    assert [c.claim for c in found] == ["Ideas converge widely.", "Fluency rose fast."]


def test_a_wikilink_is_a_cross_reference_only_at_the_end_or_before_a_colon():
    for text in (
        "Divergent thinking is stronger in [[children]] (Smith, 2020).",
        "- [[Homogenization]] \u2014 ideas converge (Anderson, 2024).",
    ):
        [cited] = claims.cited_claims(text)
        assert cited.error is None, text
    for text in (
        "Both are covered in [[some-note]] (Smith, 2020).",
        "- [[some-note]]: the full treatment (Smith, 2020).",
        "These are summarised in [[some-note]] (Smith, 2020).",
    ):
        [cited] = claims.cited_claims(text)
        assert cited.error == "not_a_claim", text


async def test_a_list_doi_that_is_not_in_the_library_drops_a_candidate_with_another_doi(session):
    await add(session, "Other doi paper", ["Ann Listdoi"], 1987, doi="10.1000/m46-b")
    note = "It holds in general (Listdoi, 1987).\n\n## References\n\n- Listdoi, A. (1987). T. https://doi.org/10.1000/m46-a\n"
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert (item.error, item.check) == ("paper_not_in_library", None)


async def test_a_key_two_list_entries_share_is_ambiguous_and_never_checked(session):
    await add(session, "The only Sharedkey paper", ["Ann Sharedkey"], 1987)
    note = (
        "It holds in general (Sharedkey, 1987).\n\n## References\n\n"
        "- Sharedkey, A. (1987). One. https://doi.org/10.1000/m46-one\n- Sharedkey, B. (1987). Two.\n"
    )
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert (item.error, item.check, item.candidates) == ("ambiguous_citation", None, ["The only Sharedkey paper"])


async def test_a_doi_paper_outside_the_workspace_is_not_matched_but_author_year_inside_it_is(session):
    await add(session, "Outside by doi", ["Ann Wsscope"], 1987, doi="10.1000/m46-ws")
    inside = await add(session, "Inside by name", ["Ann Wsscope"], 1987)
    workspace = await workspaces.create(session, "m46 scope")
    await workspaces.add_paper(session, workspace.id, inside.id)
    note = "It holds in general (Wsscope, 1987).\n\n## References\n\n- Wsscope, A. (1987). T. https://doi.org/10.1000/m46-ws\n"
    [item] = await claims.verify_claims(session, note, workspace_id=workspace.id, llm=Scripted())
    assert item.check.paper_id == inside.id


@pytest.mark.parametrize(
    "sentence, via",
    [
        (
            "It holds broadly here (Runco & Jaeger, 2012, p. 92, as quoted in Abraham, 2025).",
            "Runco & Jaeger, 2012, p. 92",
        ),
        ("It holds broadly here (Barron, 1955, as cited in Abraham, 2025).", "Barron, 1955"),
        ("It holds broadly here (Boden, 2004, as summarised in Abraham, 2025).", "Boden, 2004"),
        ("It holds broadly here (Boden, 2004, as summarized by Abraham, 2025).", "Boden, 2004"),
    ],
)
def test_a_secondary_citation_points_at_the_source_read(sentence, via):
    [found] = claims.cited_claims(sentence)
    assert (found.citation.surname, found.citation.year, found.citation.via) == ("Abraham", 2025, via)
    assert found.claim == "It holds broadly here."


def test_a_standalone_quoted_in_supersedes_the_narrative_original():
    [found] = claims.cited_claims('Stein (1953) wrote: "The creative work is a novel work" (quoted in Abraham, 2025).')
    assert (found.citation.surname, found.citation.via) == ("Abraham", "Stein (1953)")
    assert found.claim == 'Stein wrote: "The creative work is a novel work".'


def test_a_secondary_source_outside_the_library_is_reported_against_that_source():
    # never silently checked against the original instead
    [found] = claims.cited_claims("It holds broadly here (Barron, 1955, as cited in Nobodyxyz, 1999).")
    assert found.citation.surname == "Nobodyxyz"


def test_cited_in_ordinary_prose_is_not_a_secondary_marker():
    [found] = claims.cited_claims("This result was widely cited in later work (Lee, 2021).")
    assert (found.citation.surname, found.citation.via) == ("Lee", None)


def test_the_pilot_secondary_citations_go_to_abraham():
    found = pilot("definition.md")
    assert len(found) == 20
    to_abraham = [c for c in found if c.citation.via]
    assert {c.citation.via for c in to_abraham} >= {"Runco & Jaeger, 2012, p. 92", "Barron, 1955", "Boden, 2004"}
    assert all((c.citation.surname, c.citation.year) == ("Abraham", 2025) for c in to_abraham)


@pytest.mark.parametrize(
    "sentence, surname, year, via",
    [
        ("It holds broadly here (Smith, 2020, discussed in detail below).", "Smith", 2020, None),
        ("It holds broadly here (see Lee, 2019, cited by many).", "Lee", 2019, None),
        ("It holds broadly here (Lee, 2019, described by Kim, 2020).", "Kim", 2020, "Lee, 2019"),
    ],
)
def test_a_secondary_marker_needs_a_cited_source_with_a_year(sentence, surname, year, via):
    [found] = claims.cited_claims(sentence)
    assert (found.citation.surname, found.citation.year, found.citation.via) == (surname, year, via)


def test_coauthors_named_in_a_citation():
    assert citations.find("It holds (Lu, Wang et al., 2025).")[0].coauthors == ("Wang",)
    assert citations.find("Doshi & Hauser (2024) found it.")[0].coauthors == ("Hauser",)
    assert citations.find("Smith et al. (2020) found it.")[0].coauthors == ()


async def test_a_coauthor_picks_one_of_two_shared_reference_entries(session):
    wang = await add(session, "Coauth NeoCoder", ["Yao Coauthlu", "Dan Wang"], 1987, doi="10.1000/m48-neo")
    await add(session, "Coauth Salieri", ["Xi Coauthlu", "Mia Sclar"], 1987, doi="10.1000/m48-sal")
    refs = (
        "\n\n## References\n\n- Coauthlu, X., Sclar, M. (1987). Salieri. https://doi.org/10.1000/m48-sal\n"
        "- Coauthlu, Y., Wang, D. (1987). NeoCoder. https://doi.org/10.1000/m48-neo\n"
    )
    note = "Codeforces problems were used here (Coauthlu, Wang et al., 1987)." + refs
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert (item.error, item.check.paper_id) == (None, wang.id)


async def test_a_referenced_paper_missing_from_the_library_is_paper_not_in_library(session):
    note = (
        "Corazza proposes a dynamic definition here (Nolibrary, 1987).\n\n## References\n\n"
        "- Nolibrary, G. E. (1987). Potential originality. https://doi.org/10.1000/m48-absent\n"
        "- Bookonly, M. A. (1987). *The creative mind*. Routledge.\n"
    )
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert (item.error, item.doi) == ("paper_not_in_library", "10.1000/m48-absent")
    swapped = note.replace("(Nolibrary, 1987)", "(Bookonly, 1987)")
    [book] = await claims.verify_claims(session, swapped, llm=Scripted())
    assert (book.error, book.doi) == ("paper_not_in_library", None)


async def test_a_citation_in_no_reference_list_is_still_citation_not_found(session):
    [item] = await claims.verify_claims(session, "It holds broadly here (Nolistxyz, 1987).", llm=Scripted())
    assert (item.error, item.doi) == ("citation_not_found", None)


async def test_a_coauthor_matching_no_entry_leaves_a_shared_key_ambiguous(session):
    await add(session, "Zeta Narrow", ["Ann Zetanarrow"], 1987)
    note = (
        "It holds in general (Zetanarrow, Kim et al., 1987).\n\n## References\n\n"
        "- Zetanarrow, A., Lee, B. (1987). One.\n- Zetanarrow, C., Wang, D. (1987). Two.\n"
    )
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert (item.error, item.check) == ("ambiguous_citation", None)


async def test_a_coauthor_is_a_whole_surname_not_a_suffix(session):
    await add(session, "Lili Paper", ["Ann Lilishared"], 1987)
    note = (
        "It holds in general (Lilishared, Li et al., 1987).\n\n## References\n\n"
        "- Lilishared, A., Lili, B. (1987). One.\n- Lilishared, C., Wang, D. (1987). Two.\n"
    )
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert (item.error, item.check) == ("ambiguous_citation", None)


async def test_a_single_entry_with_full_first_names_keeps_its_doi(session):
    paper = await add(session, "Fullname Paper", ["Jon Fullnamesmith", "Kim Lee"], 1987, doi="10.1000/m48-full")
    await add(session, "Fullname Decoy", ["Jon Fullnamesmith"], 1987, doi="10.1000/m48-decoy")
    note = (
        "It holds in general (Fullnamesmith & Lee, 1987).\n\n## References\n\n"
        "- Fullnamesmith, Jon, & Lee, Kim (1987). T. https://doi.org/10.1000/m48-full\n"
    )
    [item] = await claims.verify_claims(session, note, llm=Scripted())
    assert (item.error, item.check.paper_id) == (None, paper.id)


async def test_a_paper_outside_the_workspace_is_paper_not_in_workspace(session):
    await add(session, "Outside Paper", ["Ann Outsidews"], 1987)
    workspace = await workspaces.create(session, "m48 empty scope")
    for note in ("It holds in general (Outsidews, 1987).",
                 "It holds in general (Outsidews, 1987).\n\n## References\n\n- Outsidews, A. (1987). T.\n"):
        [item] = await claims.verify_claims(session, note, workspace_id=workspace.id, llm=Scripted())
        assert item.error == "paper_not_in_workspace"
    [gone] = await claims.verify_claims(session, "It holds in general (Nowherews, 1987).",
                                        workspace_id=workspace.id, llm=Scripted())
    assert gone.error == "citation_not_found"


async def test_coauthors_narrow_two_library_candidates(session):
    wang = await add(session, "Narrow NeoCoder", ["Yao Narrowlu", "Dan Wang"], 1987)
    await add(session, "Narrow Salieri", ["Xi Narrowlu", "Mia Sclar"], 1987)
    cite = citations.find("It holds (Narrowlu, Wang et al., 1987).")[0]
    assert (await citations.match(session, cite)).paper_ids == [wang.id]


async def test_a_coauthor_never_drops_a_lone_match(session):
    only_first = await add(session, "Lone paper", ["Ann Lonefirst"], 1987)  # truncated author list
    cite = citations.find("It holds (Lonefirst, Nothere et al., 1987).")[0]
    assert (await citations.match(session, cite)).paper_ids == [only_first.id]


async def test_coauthors_that_match_nobody_leave_the_ambiguity(session):
    await add(session, "Twin one", ["Ann Twincoauth", "Bo Alpha"], 1987)
    await add(session, "Twin two", ["Ann Twincoauth", "Cy Beta"], 1987)
    cite = citations.find("It holds (Twincoauth, Gamma et al., 1987).")[0]
    assert len((await citations.match(session, cite)).paper_ids) == 2


async def test_a_coauthor_is_a_whole_surname_never_a_suffix(session):
    await add(session, "Lili paper", ["Ann Narrowsx", "Ann Lili"], 1987)
    wei = await add(session, "Wei paper", ["Ann Narrowsx", "Wei Li"], 1987)
    cite = citations.find("It holds (Narrowsx, Li et al., 1987).")[0]
    assert (await citations.match(session, cite)).paper_ids == [wei.id]
