import uuid

import pytest
from sqlalchemy import select

from app.core import notes_markdown as nm
from app.core.errors import InvalidInput
from app.models import Note, Paper, note_anchors, note_papers

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

RUN = uuid.uuid4().hex[:8]


async def make_paper(session, doi) -> Paper:
    paper = Paper(title=f"Import {RUN}", file_path="/x.pdf", page_count=1, doi=doi)
    session.add(paper)
    await session.commit()
    return paper


def md(name, text):
    return (name, text.encode())


async def links(session, body):
    note = await session.scalar(select(Note).where(Note.body == body))
    rows = await session.scalars(select(note_papers.c.paper_id).where(note_papers.c.note_id == note.id))
    return note, set(rows)


async def test_doi_in_frontmatter_links_and_frontmatter_is_stripped(session):
    paper = await make_paper(session, f"10.7777/fm-{RUN}")
    body = f"# My title {RUN}\n\ntext"
    result = await nm.import_files(session, [md("a.md", f"---\ndoi: 10.7777/FM-{RUN}\ntags: [x]\n---\n{body}\n")])
    assert (result.created, result.linked_papers, result.unmatched_files, result.skipped_duplicates) == (1, 1, 0, 0)
    note, linked = await links(session, body)
    assert note.provenance == "human" and linked == {paper.id}
    assert not (await session.execute(select(note_anchors).where(note_anchors.c.note_id == note.id))).all()


async def test_doi_in_body_strips_trailing_punctuation_and_matches_case_insensitively(session):
    paper = await make_paper(session, f"10.7777/body-{RUN}")
    body = f"See (https://doi.org/10.7777/BODY-{RUN}). Also [10.7777/none-{RUN}]; done"
    result = await nm.import_files(session, [md("b.md", body)])
    assert result.linked_papers == 1 and (await links(session, body))[1] == {paper.id}


async def test_no_match_still_creates_the_note_and_counts_it(session):
    body = f"nothing to link {RUN}"
    result = await nm.import_files(session, [md("c.md", body)])
    assert (result.created, result.linked_papers, result.unmatched_files) == (1, 0, 1)
    assert (await links(session, body))[1] == set()


async def test_importing_twice_skips_exact_duplicates(session):
    body = f"same body {RUN}"
    await nm.import_files(session, [md("d.md", body)])
    again = await nm.import_files(session, [md("d.md", body), md("e.md", body + " changed")])
    assert (again.created, again.skipped_duplicates) == (1, 1)


@pytest.mark.parametrize(
    "files",
    [
        [("a.txt", b"x")],
        [("a.md", b"\xff\xfe\x00bad")],
        [("big.md", b"x" * (1024 * 1024 + 1))],
        [(f"{i}.md", b"x") for i in range(51)],
        [("empty.md", b"  \n")],
    ],
    ids=["extension", "utf8", "size", "count", "empty"],
)
async def test_limits_raise_invalid_input(session, files):
    with pytest.raises(InvalidInput):
        await nm.import_files(session, files)


async def test_http_import_and_422s(client, session):
    paper = await make_paper(session, f"10.7777/http-{RUN}")
    body = f"over http 10.7777/http-{RUN} {RUN}"
    ok = await client.post("/api/notes/import", files=[("files", ("n.md", body.encode(), "text/markdown"))])
    assert ok.status_code == 200
    assert ok.json() == {"created": 1, "linked_papers": 1, "unmatched_files": 0, "skipped_duplicates": 0}
    assert (await links(session, body))[1] == {paper.id}
    bad = await client.post("/api/notes/import", files=[("files", ("n.txt", b"x", "text/plain"))])
    assert bad.status_code == 422 and "Markdown" in bad.json()["detail"]
