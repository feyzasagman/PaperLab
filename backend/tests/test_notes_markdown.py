import io
import uuid
import zipfile
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import insert

from app.core import notes_markdown as nm
from app.models import Note, Paper, note_anchors, note_papers

pytestmark = pytest.mark.anyio

PID, CID, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def paper(**kw):
    base = dict(id=PID, title="Attention Is All", doi="10.1/abc", year=2017, authors=["Ashish Vaswani", "Noam Shazeer"],
                source_type="preprint", venue=None, oa_url=None, peer_reviewed=None, evidence_access=None)  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def note(body="Key claim\nmore", provenance="human", anchors=(), paper_ids=(PID,)):
    return nm.NoteItem(body, provenance, list(paper_ids), list(anchors))


def anchor(page=4, quote="a quote", chunk=CID, paper_id=PID):
    return nm.AnchorItem(paper_id, page, chunk, quote)


def render(notes, p=None, keys=None, origin="http://app"):
    p = p or paper()
    return nm.render_paper(p, "vaswani2017attention", notes, keys or {str(p.id): "vaswani2017attention"}, origin)


def frontmatter(text):
    return yaml.safe_load(text.split("---\n")[1])


def test_frontmatter_round_trips_with_the_right_fields():
    meta = frontmatter(render([]))
    assert meta == {
        "title": "Attention Is All",
        "doi": "10.1/abc",
        "year": 2017,
        "authors": ["Vaswani, Ashish", "Shazeer, Noam"],
        "source_type": "preprint",
        "paperlab": f"paperlab://papers/{PID}",
        "paperlab_web": f"http://app/#/papers/{PID}",
        "tags": ["paperlab"],
    }


def test_frontmatter_survives_hostile_titles_and_omits_missing_fields():
    meta = frontmatter(render([], paper(title='A: "b" # c\n- d', doi=None, year=None, authors=[])))
    assert meta["title"] == 'A: "b" # c\n- d'
    assert not {"doi", "year", "authors"} & meta.keys()


def test_paper_with_no_notes_says_so():
    assert "No notes yet." in render([])


def test_note_block_has_heading_label_page_quote_and_link():
    text = render([note(anchors=[anchor()])])
    assert "### Key claim\n" in text
    assert "*You* · p. 4" in text
    assert "\n> a quote\n" in text
    assert f"[Open in PaperLab](paperlab://papers/{PID}?page=4&chunk={CID})" in text
    assert text.rstrip().endswith("Key claim\nmore")


def test_author_labels():
    assert "*AI*" in render([note(provenance="llm")]) and "*AI · edited*" in render([note(provenance="llm_edited")])


def test_quote_is_verbatim_and_every_line_stays_in_the_blockquote():
    text = render([note(anchors=[anchor(quote="line one\n\n# not a heading\nline three")])])
    assert "> line one\n>\n> # not a heading\n> line three\n" in text


def test_unanchored_note_has_no_page_quote_or_link():
    block = render([note()]).split("### Key claim")[1]
    assert "*You*" in block and "p. " not in block and "> " not in block and "Open in PaperLab" not in block


def test_note_on_two_papers_lists_the_other_in_the_workspace_only():
    keys = {str(PID): "vaswani2017attention", str(OTHER): "other2020thing"}
    text = render([note(paper_ids=[PID, OTHER, uuid.uuid4()])], keys=keys)
    assert "Also on: [[other2020thing]]" in text and text.count("Also on") == 1
    assert "Also on" not in render([note()], keys=keys)


def test_headings_neutralise_hash_and_wikilinks_and_cap_at_80():
    text = render([note(body="## C# [[x]] tips\nbody"), note(body="y" * 200)])
    assert "### C\\# [x] tips\n" in text
    assert f"### {'y' * 80}\n" in text and "y" * 81 not in text.split("### ")[2].split("\n")[0]


def test_index_lists_wikilinks_and_the_question():
    text = nm.render_index("My [[ws]]", "Does it work?", [("a2020x", "Title # one")])
    assert text.startswith("# My [ws]\n") and "Does it work?" in text and "- [[a2020x]] Title \\# one" in text


async def seed(session, client):
    ws = (await client.post("/api/workspaces", json={"name": f"Notes MD {uuid.uuid4().hex[:8]} é"})).json()
    p1 = Paper(title="Zeta paper", file_path="/x.pdf", page_count=3, year=2020, authors=["Jane Smith"], doi="10.9/zz")
    p2 = Paper(title="Empty one", file_path="/x.pdf", page_count=3, year=2021, authors=["Jane Smith"])
    session.add_all([p1, p2])
    await session.commit()
    for p in (p1, p2):
        await client.put(f"/api/workspaces/{ws['id']}/papers/{p.id}")
    n = Note(body="First thought\ndetails", provenance="human")
    session.add(n)
    await session.flush()
    await session.execute(insert(note_papers).values([{"note_id": n.id, "paper_id": p1.id}]))
    await session.execute(
        insert(note_anchors).values(
            note_id=n.id, paper_id=p1.id, page=2, bbox=[[1, 2, 3, 4]], quoted_text="verbatim\nquote"
        )
    )
    await session.commit()
    return ws, p1, p2


async def test_zip_layout_and_content_over_http(client, session):
    ws, p1, p2 = await seed(session, client)
    response = await client.get(f"/api/workspaces/{ws['id']}/notes.zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    slug = disposition.split('filename="')[1].removesuffix('-notes.zip"')
    assert disposition.startswith('attachment; filename="notes-md-') and slug
    files = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(files.namelist())
    assert names == sorted([f"{slug}/index.md", f"{slug}/smith2020zeta.md", f"{slug}/smith2021empty.md"])
    assert "> verbatim\n> quote" in files.read(f"{slug}/smith2020zeta.md").decode()
    assert "No notes yet." in files.read(f"{slug}/smith2021empty.md").decode()
    assert "[[smith2020zeta]]" in files.read(f"{slug}/index.md").decode()
    assert f"http://test/#/papers/{p1.id}" in files.read(f"{slug}/smith2020zeta.md").decode()
    await client.delete(f"/api/workspaces/{ws['id']}")


async def test_unknown_workspace_is_404(client):
    assert (await client.get(f"/api/workspaces/{uuid.uuid4()}/notes.zip")).status_code == 404
