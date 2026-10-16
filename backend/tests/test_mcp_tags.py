"""M58 MCP tag tools and list_papers by tag, through an in-process client against the test DB."""

import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from pdf_papers import TWO_LINE_QUOTE, chunked_paper
from screening_pool import make_pool
from sqlalchemy import select

from app.core import discovery, tags, workspace_search, workspaces
from app.core.paper_sources import SourceSettings
from app.core.source_registry import SOURCE_IDS
from app.models import Paper, hit_tags
from app.providers import discovery_fake, embedding

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]
RUN = uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def fake_query_embedder(embedder, monkeypatch):
    monkeypatch.setattr(embedding, "get_model", lambda: embedder)


async def ten_papers(session, name):
    ws = await workspaces.create(session, name)
    papers = []
    for _ in range(10):
        paper = Paper(title="P", file_path="/nonexistent.pdf", page_count=1, doi=f"10.5555/{uuid.uuid4().hex}")
        session.add(paper)
        await session.commit()
        await workspaces.add_paper(session, ws.id, paper.id)
        papers.append(paper)
    return papers


async def test_tag_list_filter_rename_and_delete_over_mcp(session):
    name = f"Tags {RUN}"
    papers = await ten_papers(session, name)
    dois = [p.doi for p in papers]

    done = result(await call("tag_papers", workspace=name, tag="M1", dois=dois))
    assert (done["tag"], done["changed"], done["unchanged"], done["skipped"]) == ("M1", 10, 0, [])
    await call("tag_papers", workspace=name, tag="Q1-techniques", paper_ids=[str(p.id) for p in papers[:6]])
    await call("tag_papers", workspace=name, tag="Q2-diversity", paper_ids=[str(p.id) for p in papers[4:]])

    counts = {t["name"]: t["papers"] for t in result(await call("list_tags", workspace=name))}
    assert counts == {"M1": 10, "Q1-techniques": 6, "Q2-diversity": 6}

    both = {"tags": ["Q1-techniques", "Q2-diversity"]}
    overlap = result(await call("list_papers", workspace=name, match="all", **both))
    assert {r["id"] for r in overlap} == {str(p.id) for p in papers[4:6]}
    union = result(await call("list_papers", workspace=name, match="any", **both))
    assert len(union) == 10

    nope = await call("list_papers", workspace=name, tags=["nope"])
    assert nope.is_error and nope.structured_content["error"] == "unknown_tag"
    assert "M1" in nope.structured_content["available"]

    await call("untag_papers", workspace=name, tag="M1", paper_ids=[str(papers[0].id)])
    renamed = result(await call("rename_tag", workspace=name, tag="m1", new_name="Milestone"))
    assert renamed == {"old": "M1", "new": "Milestone"}
    gone = result(await call("delete_tag", workspace=name, tag="Milestone"))
    assert gone == {"tag": "Milestone", "papers": 9}
    assert len(result(await call("list_papers", workspace=name))) == 10


async def two_tagged_papers(session, tmp_path, name):
    """Two chunked papers in one workspace; only the first carries Q2-diversity."""
    tagged, tagged_chunks, vectors = await chunked_paper(session, tmp_path / "a", title="Tagged paper")
    other, _, _ = await chunked_paper(session, tmp_path / "b", title="Untagged paper")
    ws = await workspaces.create(session, name)
    for paper in (tagged, other):
        await workspaces.add_paper(session, ws.id, paper.id)
    await call("tag_papers", workspace=name, tag="Q2-diversity", paper_ids=[str(tagged.id)])
    return tagged, other, tagged_chunks, vectors


async def test_search_library_by_tag_returns_only_tagged_passages(session, tmp_path, embedder):
    name = f"Tag search {RUN}"
    tagged, _, _, vectors = await two_tagged_papers(session, tmp_path, name)
    embedder.vectors["search_query: how are notes anchored?"] = vectors[0]

    found = result(await call("search_library", query="how are notes anchored?", workspace=name, tags=["q2-diversity"]))

    assert found and {p["paper_id"] for p in found} == {str(tagged.id)}


async def test_export_citations_by_tag_lists_only_those_papers(session, tmp_path):
    name = f"Tag cite {RUN}"
    await two_tagged_papers(session, tmp_path, name)

    cited = result(await call("export_citations", workspace=name, tags=["Q2-diversity"]))

    assert cited["count"] == 1 and "Tagged paper" in cited["text"] and "Untagged paper" not in cited["text"]


async def test_list_evidence_cards_and_notes_by_tag(session, tmp_path):
    name = f"Tag cards {RUN}"
    tagged, other, _, _ = await two_tagged_papers(session, tmp_path, name)
    for paper in (tagged, other):
        made = await call("create_note", body="n", paper_id=str(paper.id), quoted_text=TWO_LINE_QUOTE)
        assert not made.is_error, made.content

    cards = result(await call("list_evidence_cards", workspace=name, tags=["Q2-diversity"]))
    notes = result(await call("list_notes", workspace=name, tags=["Q2-diversity"]))

    assert [c["paper_id"] for c in cards] == [str(tagged.id)]
    assert [n["paper_ids"] for n in notes] == [[str(tagged.id)]]
    assert len(result(await call("list_notes", workspace=name))) == 2


async def test_tags_without_a_workspace_are_refused(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path)

    searched = await call("search_library", query="anything", tags=["x"])
    noted = await call("list_notes", paper_id=str(paper.id), tags=["x"])

    for refused in (searched, noted):
        assert refused.is_error and refused.structured_content["error"] == "tags_need_a_workspace"


async def _free_hit(session, label):
    """A workspace with one snowball hit whose PDF is free (the `...free` DOI of the offline fakes)."""
    ws = await workspaces.create(session, f"{label} {RUN}")
    paper = Paper(title="Seed", file_path="/nonexistent.pdf", page_count=1, doi=f"10.5555/{uuid.uuid4().hex}")
    session.add(paper)
    await session.commit()
    await workspaces.add_paper(session, ws.id, paper.id)
    every = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCE_IDS, True))
    providers = discovery.build_providers(every, discovery_fake.transport())
    try:
        await workspace_search.snowball(session, providers, ws.id, [paper.id], [], backward=True, forward=False)
    finally:
        await providers.aclose()
    hits = result(await call("list_hits", workspace=ws.name))["hits"]
    return ws, next(h for h in hits if h["doi"] == f"{discovery_fake.DOI_PREFIX}free")


async def test_review_then_import_lands_the_paper_under_its_tag(session):
    ws, free = await _free_hit(session, "Land")
    reviewed = result(
        await call("review_hits", workspace=ws.name, hit_ids=[free["id"]], decision="relevant", tags=["Q1-techniques"])
    )
    assert reviewed["updated"] == 1
    imported = result(await call("import_hit", workspace=ws.name, hit_id=free["id"]))
    listed = result(await call("list_papers", workspace=ws.name, tags=["q1-techniques"]))
    assert [r["id"] for r in listed] == imported["paper_ids"]


async def test_import_hit_with_tags_and_an_already_imported_hit(session):
    ws, free = await _free_hit(session, "Both")
    imported = result(await call("import_hit", workspace=ws.name, hit_id=free["id"], tags=["Q2"]))
    assert [r["id"] for r in result(await call("list_papers", workspace=ws.name, tags=["Q2"]))] == imported["paper_ids"]
    # The hit is imported now: tagging it at review tags its paper at once.
    await call("review_hits", workspace=ws.name, hit_ids=[free["id"]], decision="relevant", tags=["Q3"])
    assert [r["id"] for r in result(await call("list_papers", workspace=ws.name, tags=["Q3"]))] == imported["paper_ids"]


async def test_skipped_hits_get_no_tags(session):
    ws, _ = await make_pool(session, [("a", None)])
    other, (foreign,) = await make_pool(session, [("b", None)])
    out = result(await call("review_hits", workspace=ws.name, hit_ids=[str(foreign.id)], decision="maybe", tags=["T"]))
    assert out == {"updated": 0, "skipped": [str(foreign.id)]}
    assert (await session.execute(select(hit_tags).where(hit_tags.c.hit_id == foreign.id))).all() == []


async def test_tag_hits_survives_thousands_of_ids(session):
    ws, hits = await make_pool(session, [("a", None)] * 3)
    ids = [uuid.uuid4() for _ in range(40000)] + [h.id for h in hits]
    await tags.tag_hits(session, ws.id, ["A", "B", "C", "D"], ids)
    mine = [h.id for h in hits]
    rows = (await session.execute(select(hit_tags.c.hit_id).where(hit_tags.c.hit_id.in_(mine)))).scalars().all()
    assert len(rows) == 12 and set(rows) == {h.id for h in hits}


async def test_an_invalid_tag_name_refuses_before_any_decision_is_written(session):
    ws, (hit,) = await make_pool(session, [("a", None)])
    out = await call("review_hits", workspace=ws.name, hit_ids=[str(hit.id)], decision="relevant", tags=[""])
    assert out.is_error and out.structured_content["error"] == "invalid_tag_name"
    await session.refresh(hit)
    assert hit.stage1_status is None


async def test_accept_suggestions_with_tags(session):
    ws, (inc, none) = await make_pool(session, [("a", None), ("b", None)])
    inc.suggestion = "include"
    await session.flush()
    out = result(await call("accept_suggestions", workspace=ws.name, hit_ids=[str(inc.id), str(none.id)], tags=["T"]))
    assert out["accepted"] == 1
    mine = [inc.id, none.id]
    tagged = (await session.execute(select(hit_tags.c.hit_id).where(hit_tags.c.hit_id.in_(mine)))).scalars().all()
    assert tagged == [inc.id]


async def test_list_papers_without_full_text(session, tmp_path):
    name = f"Full text {RUN}"
    ws = await workspaces.create(session, name)
    full, _, _ = await chunked_paper(session, tmp_path / "full", title="Has full text")
    bare = Paper(title="Metadata only", file_path="/nonexistent.pdf", page_count=1)
    session.add(bare)
    await session.commit()
    for paper in (full, bare):
        await workspaces.add_paper(session, ws.id, paper.id)
    await call("tag_papers", workspace=name, tag="Q1", paper_ids=[str(full.id), str(bare.id)])

    session.expire_all()  # the test session shares its identity map with the server; has_chunks was loaded before chunking

    def ids(rows):
        return {r["id"] for r in result(rows)}

    assert ids(await call("list_papers", workspace=name, full_text=False)) == {str(bare.id)}
    assert ids(await call("list_papers", workspace=name, full_text=True)) == {str(full.id)}
    assert ids(await call("list_papers", workspace=name)) == {str(full.id), str(bare.id)}
    assert ids(await call("list_papers", workspace=name, full_text=False, tags=["Q1"])) == {str(bare.id)}
