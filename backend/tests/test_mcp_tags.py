"""M58 MCP tag tools and list_papers by tag, through an in-process client against the test DB."""

import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result

from app.core import workspaces
from app.models import Paper

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]
RUN = uuid.uuid4().hex[:8]


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
