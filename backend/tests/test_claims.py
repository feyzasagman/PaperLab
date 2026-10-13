import json

import pytest
from pdf_papers import TWO_LINE_QUOTE, chunked_paper
from sqlalchemy import select

from app.core import claims, notes, quotes, retrieval
from app.core.errors import InvalidInput, NotFound
from app.models import LLMOutput, Paper, PaperStatus

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

ABSTRACT = "We study how large language models homogenize creative writing. Outputs were 23% less diverse."


class Scripted:
    model, connection_name = "fake:scripted", "Test"

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    async def stream(self, system, prompt):
        self.prompts.append(prompt)
        yield self.replies.pop(0)


def reply(verdict="supported", passage=None, quote="", reason="It says so."):
    return json.dumps({"verdict": verdict, "passage": passage, "quote": quote, "reason": reason})


@pytest.fixture(autouse=True)
def search_with_the_test_embedder(embedder, monkeypatch):
    async def query_embedder(session, given=None):
        return embedder

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)


async def bare_paper(session, **fields):
    paper = Paper(title="Bare", file_path="x.pdf", status=PaperStatus.READY, **fields)
    session.add(paper)
    await session.flush()
    return paper


async def test_full_text_verdict_with_a_kept_quote_and_its_provenance_row(session, tmp_path):
    paper, chunks, _ = await chunked_paper(session, tmp_path)
    llm = Scripted(reply(passage=1, quote=TWO_LINE_QUOTE))
    # the quote is in the second paragraph's chunk; point at whichever passage number holds it
    text_of = {c.id: c.text for c in chunks}
    check = await claims.verify_claim(session, "Models cite their passages.", paper_id=paper.id, llm=llm)

    assert check.verdict == "supported" and check.evidence_level == "full_text" and check.confidence == "high"
    assert quotes.fold(TWO_LINE_QUOTE) in quotes.fold(text_of[check.quote_chunk_id])
    assert check.quote == TWO_LINE_QUOTE and check.quote_page == 1 and check.quote_section == "1 Introduction"
    assert {p["chunk_id"] for p in check.passages} == {c.id for c in chunks}
    assert "(page 1, section 1 Introduction)" in llm.prompts[0] and "[6]" not in llm.prompts[0]
    row = await session.get(LLMOutput, check.output_id)
    assert (row.kind, row.paper_id, row.model, row.prompt_version) == ("claim_check", paper.id, "fake:scripted", 1)
    assert row.source_chunks == [p["chunk_id"] for p in check.passages]
    assert json.loads(row.content)["verdict"] == "supported"


async def test_a_quote_naming_the_wrong_passage_is_kept_and_the_passage_corrected(session, tmp_path):
    paper, chunks, _ = await chunked_paper(session, tmp_path)
    holder = next(c for c in chunks if quotes.fold(TWO_LINE_QUOTE) in quotes.fold(c.text))
    other = next(c for c in chunks if c.id != holder.id)

    async def shown_number(chunk):
        probe = await claims.verify_claim(session, "c", paper_id=paper.id, llm=Scripted(reply("not_addressed")))
        return [p["chunk_id"] for p in probe.passages].index(chunk.id) + 1

    wrong = await shown_number(other)
    check = await claims.verify_claim(
        session, "c", paper_id=paper.id, llm=Scripted(reply(passage=wrong, quote=TWO_LINE_QUOTE))
    )
    # passages come back in distance order, which the random vectors may reshuffle between calls: assert on the chunk
    assert check.quote == TWO_LINE_QUOTE and check.quote_chunk_id == holder.id and check.confidence == "high"


async def test_an_out_of_range_passage_number_still_finds_the_quote(session, tmp_path):
    paper, chunks, _ = await chunked_paper(session, tmp_path)
    check = await claims.verify_claim(
        session, "c", paper_id=paper.id, llm=Scripted(reply(passage=99, quote=TWO_LINE_QUOTE))
    )
    assert check.quote == TWO_LINE_QUOTE and check.confidence == "high"


async def test_an_invented_quote_is_dropped_and_the_check_is_low(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path)
    check = await claims.verify_claim(
        session, "c", paper_id=paper.id, llm=Scripted(reply(quote="a sentence the paper never wrote"))
    )
    assert check.verdict == "supported" and check.quote is None and check.quote_chunk_id is None
    assert check.confidence == "low"


async def test_supported_with_no_quote_is_low_but_not_addressed_with_none_is_high(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path)
    supported = await claims.verify_claim(session, "c", paper_id=paper.id, llm=Scripted(reply("supported")))
    silent = await claims.verify_claim(session, "c", paper_id=paper.id, llm=Scripted(reply("not_addressed")))
    assert (supported.confidence, silent.confidence) == ("low", "high")


async def test_abstract_only_uses_the_abstract_as_the_one_passage(session):
    paper = await bare_paper(session, abstract=ABSTRACT)
    llm = Scripted(reply(passage=1, quote="Outputs were 23% less diverse"))
    check = await claims.verify_claim(session, "LLMs make writing less diverse.", paper_id=paper.id, llm=llm)
    assert check.evidence_level == "abstract_only" and check.quote == "Outputs were 23% less diverse"
    assert check.passages == [{"chunk_id": None, "page": None, "section": None}] and check.quote_page is None
    assert check.confidence == "high" and "[1] (abstract)" in llm.prompts[0]
    assert (await session.get(LLMOutput, check.output_id)).source_chunks == []


async def test_metadata_only_cannot_verify_without_asking_the_model_or_writing_a_row(session):
    paper = await bare_paper(session)
    llm = Scripted()
    check = await claims.verify_claim(session, "c", paper_id=paper.id, llm=llm)
    assert (check.verdict, check.reason, check.evidence_level) == ("cannot_verify", "no_text", "metadata_only")
    assert llm.prompts == [] and check.output_id is None
    assert await session.scalar(select(LLMOutput).where(LLMOutput.paper_id == paper.id)) is None


async def test_doi_lookup_ignores_case_and_a_missing_doi_is_not_in_the_library(session):
    paper = await bare_paper(session, abstract=ABSTRACT, doi="10.1234/claims-abc")
    check = await claims.verify_claim(session, "c", doi=" 10.1234/CLAIMS-ABC ", llm=Scripted(reply("not_addressed")))
    assert check.paper_id == paper.id
    with pytest.raises(NotFound, match="paper_not_in_library") as missing:
        await claims.verify_claim(session, "c", doi="10.1234/nope", llm=Scripted())
    assert missing.value.details["doi"] == "10.1234/nope"


@pytest.mark.parametrize("scope", [{}, {"paper_id": "x", "doi": "10.1/x"}])
async def test_both_or_neither_of_paper_and_doi_is_claim_scope(session, scope):
    with pytest.raises(InvalidInput) as raised:
        await claims.verify_claim(session, "c", llm=Scripted(), **scope)
    assert str(raised.value) == "claim_scope"


async def test_two_invalid_replies_cannot_verify_with_model_error_and_a_row_saying_so(session):
    paper = await bare_paper(session, abstract=ABSTRACT)
    check = await claims.verify_claim(session, "c", paper_id=paper.id, llm=Scripted("nope", "still nope"))
    assert (check.verdict, check.reason) == ("cannot_verify", "model_error")
    assert json.loads((await session.get(LLMOutput, check.output_id)).content)["reason"] == "model_error"


async def test_every_returned_quote_is_a_fold_substring_of_a_returned_passage(session, tmp_path):
    paper, chunks, _ = await chunked_paper(session, tmp_path)
    texts = {c.id: c.text for c in chunks}
    attempts = [TWO_LINE_QUOTE, "the reader can check every claim", "Highlights are the anchor", "two words",
                "x" * 400, "an invented quote of several words", TWO_LINE_QUOTE.upper(), ""]
    for attempt in attempts:
        for passage in (None, 1, 2, 7):
            check = await claims.verify_claim(
                session, "c", paper_id=paper.id, llm=Scripted(reply(passage=passage, quote=attempt))
            )
            if check.quote is not None:
                assert notes.fold(check.quote).replace("- ", "-") in quotes.fold(texts[check.quote_chunk_id])
                assert len(check.quote.split()) >= 3 and len(check.quote) <= 300
            else:
                assert check.confidence == "low" or attempt == ""


def test_check_quotes_applies_the_shared_rules():
    kept, dropped = quotes.check_quotes(
        ["cost- effective care for", "two words", "never in there at all", "x y z"],
        ["Cost-effective care for all", "x y z"], 5,
    )
    assert kept == [("cost- effective care for", 0), ("x y z", 1)] and dropped
    assert quotes.check_quotes(["a b c", "b c d"], ["a b c d"], 1) == ([("a b c", 0)], False)
