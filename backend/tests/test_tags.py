import uuid

import pytest
from pdf_papers import chunked_paper
from sqlalchemy import select

from app.core import tags, workspaces
from app.core.errors import Conflict, InvalidInput
from app.models import Paper, workspace_papers

pytestmark = pytest.mark.anyio

RUN = uuid.uuid4().hex[:8]


async def make_paper(session, **fields) -> Paper:
    fields.setdefault("doi", f"10.5555/{uuid.uuid4().hex}")
    paper = Paper(title="A paper", file_path="/nonexistent.pdf", page_count=5, **fields)
    session.add(paper)
    await session.commit()
    return paper


async def workspace(session, label="a"):
    return (await workspaces.create(session, f"tags-{label}-{RUN}-{uuid.uuid4().hex[:4]}")).id


async def filled(session, count=3, **fields):
    ws = await workspace(session)
    papers = [await make_paper(session, **fields) for _ in range(count)]
    for p in papers:
        await workspaces.add_paper(session, ws, p.id)
    return ws, papers


async def members(session, ws):
    return set(await session.scalars(select(workspace_papers.c.paper_id).where(workspace_papers.c.workspace_id == ws)))


async def test_tag_by_id_and_doi_creates_the_tag_once_and_skips_non_members(session):
    ws, (p1, p2, _p3) = await filled(session)
    outsider = await make_paper(session)
    dois = [p2.doi.upper(), outsider.doi, "10.9/none"]
    result = await tags.tag_papers(session, ws, "Q2-diversity", paper_ids=[p1.id], dois=dois)
    assert (result.tag, result.changed, result.unchanged) == ("Q2-diversity", 2, 0)
    assert result.skipped == [outsider.doi, "10.9/none"]
    assert outsider.id not in await members(session, ws)
    again = await tags.tag_papers(session, ws, "Q2-diversity", paper_ids=[p1.id], dois=[p2.doi])
    assert (again.changed, again.unchanged) == (0, 2)


async def test_case_insensitive_names(session):
    ws, (p1, p2, _) = await filled(session)
    await tags.tag_papers(session, ws, "Q2-diversity", paper_ids=[p1.id])
    await tags.tag_papers(session, ws, "q2-diversity", paper_ids=[p2.id])
    rows = await tags.summaries(session, ws)
    assert [(r.name, r.papers) for r in rows] == [("Q2-diversity", 2)]


async def test_rename_and_delete_keep_papers(session):
    ws, (p1, p2, _) = await filled(session)
    await tags.tag_papers(session, ws, "Q2", paper_ids=[p1.id, p2.id])
    await tags.tag_papers(session, ws, "other", paper_ids=[p1.id])
    with pytest.raises(Conflict) as err:
        await tags.rename(session, ws, "Q2", "OTHER")
    assert err.value.args[0] == "tag_exists"
    assert await tags.rename(session, ws, "Q2", "q2") == "q2"
    assert await tags.delete(session, ws, "Q2") == 2
    assert {p1.id, p2.id} <= await members(session, ws)
    assert [r.name for r in await tags.summaries(session, ws)] == ["other"]


async def test_removing_a_paper_from_the_workspace_drops_its_tags_there_only(session):
    ws_a, (paper, *_) = await filled(session)
    ws_b = await workspace(session, "b")
    await workspaces.add_paper(session, ws_b, paper.id)
    for ws in (ws_a, ws_b):
        await tags.tag_papers(session, ws, "x", paper_ids=[paper.id])
    await workspaces.remove_paper(session, ws_a, paper.id)
    assert (await tags.summaries(session, ws_a))[0].papers == 0
    assert (await tags.summaries(session, ws_b))[0].papers == 1


async def test_summaries_count_review_and_access(session, tmp_path):
    ws = await workspace(session)
    reviewed = await make_paper(session, peer_reviewed="true")
    preprint = await make_paper(session, source_type="preprint")
    plain = await make_paper(session, abstract="An abstract.")
    chunked, _, _ = await chunked_paper(session, tmp_path, title="Chunked")
    chunked.peer_reviewed = "true"
    await session.commit()
    ids = [p.id for p in (reviewed, preprint, plain, chunked)]
    for pid in ids:
        await workspaces.add_paper(session, ws, pid)
    await tags.tag_papers(session, ws, "full", paper_ids=ids)
    await tags.tag_papers(session, ws, "empty", paper_ids=[ids[0]])
    await tags.untag_papers(session, ws, "empty", paper_ids=[ids[0]])
    by_name = {r.name: r for r in await tags.summaries(session, ws)}
    full = by_name["full"]
    assert (full.papers, full.peer_reviewed, full.preprint, full.unknown_review) == (4, 2, 1, 1)
    assert (full.full_text, full.abstract, full.metadata) == (1, 1, 2)
    assert by_name["empty"].papers == 0


async def test_paper_ids_any_all_and_none(session):
    ws, (p1, p2, p3) = await filled(session)
    await tags.tag_papers(session, ws, "a", paper_ids=[p1.id, p2.id])
    await tags.tag_papers(session, ws, "b", paper_ids=[p2.id, p3.id])
    assert await tags.paper_ids(session, ws, []) is None
    assert await tags.paper_ids(session, ws, None) is None
    assert await tags.paper_ids(session, ws, ["A", "b"]) == {p1.id, p2.id, p3.id}
    assert await tags.paper_ids(session, ws, ["a", "b"], "all") == {p2.id}
    assert await tags.paper_ids(session, ws, ["a"], "all") == {p1.id, p2.id}
    with pytest.raises(InvalidInput) as err:
        await tags.paper_ids(session, ws, ["nope"])
    assert err.value.args[0] == "unknown_tag" and err.value.details["available"] == ["a", "b"]
    with pytest.raises(InvalidInput) as err:
        await tags.paper_ids(session, ws, ["a"], "some")
    assert err.value.args[0] == "invalid_match"


async def test_untag_unknown_tag_is_not_created(session):
    ws, (p1, *_) = await filled(session)
    with pytest.raises(InvalidInput) as err:
        await tags.untag_papers(session, ws, "ghost", paper_ids=[p1.id])
    assert err.value.args[0] == "unknown_tag"
    assert await tags.summaries(session, ws) == []


async def test_bad_names_and_batches(session):
    ws, (p1, *_) = await filled(session)
    for bad in ("", "   ", "x" * 65):
        with pytest.raises(InvalidInput) as err:
            await tags.tag_papers(session, ws, bad, paper_ids=[p1.id])
        assert err.value.args[0] == "invalid_tag_name"
    with pytest.raises(InvalidInput) as err:
        await tags.tag_papers(session, ws, "x", paper_ids=[uuid.uuid4() for _ in range(5001)])
    assert err.value.args[0] == "too_many_papers"
    with pytest.raises(InvalidInput) as err:
        await tags.tag_papers(session, ws, "x")
    assert err.value.args[0] == "no_papers_given"
