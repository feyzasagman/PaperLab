"""M39 F1 MCP tools: workspaces, papers and adding papers, through an in-process client against the test DB."""

import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from pdf_papers import chunked_paper

from app.core import discovery, papers, pdf_links, pipeline, workspaces
from app.core.candidates import Candidate
from app.models import PaperStatus
from app.providers import discovery_fake

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]
RUN = uuid.uuid4().hex[:8]  # the owner's workspaces share the dev database (D37)


async def test_create_then_list_workspaces_with_a_description():
    created = result(await call("create_workspace", name=f"Creativity {RUN}", description="LLMs and creativity"))
    listed = result(await call("list_workspaces"))

    assert created["name"] == f"Creativity {RUN}" and created["description"] == "LLMs and creativity"
    assert any(w["name"] == f"Creativity {RUN}" and w["paper_count"] == 0 for w in listed)


async def test_create_workspace_twice_is_a_readable_conflict():
    await call("create_workspace", name=f"Twice {RUN}")
    again = await call("create_workspace", name=f"Twice {RUN}")
    assert again.is_error and again.structured_content["error"] == "workspace_name_taken"


async def test_list_papers_shows_ingest_status(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, title="Listed paper")
    ws = await workspaces.create(session, f"Papers {RUN}")
    await workspaces.add_paper(session, ws.id, paper.id)

    rows = result(await call("list_papers", workspace=f"Papers {RUN}"))

    assert [(r["id"], r["title"]) for r in rows] == [(str(paper.id), "Listed paper")]
    assert rows[0]["status"] == paper.status


async def test_list_papers_in_an_unknown_workspace_names_the_real_ones(session):
    await workspaces.create(session, f"Real {RUN}")
    response = await call("list_papers", workspace=f"Nope {RUN}")
    assert response.is_error and response.structured_content["error"] == "unknown_workspace"
    assert f"Real {RUN}" in response.structured_content["available"]


async def test_add_then_remove_a_paper_keeps_it_in_the_library(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, title="Moved paper")
    ws = await workspaces.create(session, f"Move {RUN}")

    added = result(await call("add_paper_to_workspace", workspace=f"Move {RUN}", paper_id=str(paper.id)))
    removed = result(await call("remove_paper_from_workspace", workspace=f"Move {RUN}", paper_id=str(paper.id)))

    assert str(ws.id) in added["workspace_ids"]
    assert removed == {"workspace": f"Move {RUN}", "paper_id": str(paper.id), "removed": True}
    assert await workspaces.papers(session, ws.id) == []
    assert (await papers.get_paper(session, paper.id)).id == paper.id


async def test_add_an_unknown_paper_to_a_workspace_is_not_found(session):
    await workspaces.create(session, f"Ghost {RUN}")
    response = await call("add_paper_to_workspace", workspace=f"Ghost {RUN}", paper_id=str(uuid.uuid4()))
    assert response.is_error and response.structured_content["error"] == "not_found"


FREE_DOI = f"{discovery_fake.DOI_PREFIX}free"


@pytest.mark.parametrize("identifier", [FREE_DOI, f"  https://doi.org/{FREE_DOI} ", f"doi:{FREE_DOI}"])
async def test_add_paper_by_doi_queues_one_ingest(session, jobs, identifier):
    added = result(await call("add_paper", identifier=identifier))

    assert added["status"] == "queued" and added["paper_id"]
    assert jobs.jobs == [("ingest_paper", added["paper_id"])]


async def test_add_paper_by_arxiv_id_with_version(jobs):
    added = result(await call("add_paper", identifier=f"arXiv:{discovery_fake.FREE_ARXIV_ID}v2"))
    assert added["status"] == "queued"
    assert len(jobs.jobs) == 1


async def test_add_paper_already_in_library_returns_it_without_a_new_job(session, jobs):
    first = result(await call("add_paper", identifier=FREE_DOI))
    again = result(await call("add_paper", identifier=FREE_DOI))

    assert again["paper_id"] == first["paper_id"]
    assert again["status"] in ("queued", "ingested")  # `ingested` once its status is ready
    assert len(jobs.jobs) == 1


@pytest.mark.parametrize("suffix", ["landing", "closed"])
async def test_no_free_pdf_returns_the_landing_page_and_stores_nothing(session, jobs, monkeypatch, suffix):
    # The offline fake answers every DOI lookup with the free fixture, so serve the landing/closed one directly.
    doi = f"{discovery_fake.DOI_PREFIX}{suffix}"
    page = [f"{discovery_fake.PDF_HOST}/page.html"] if suffix == "landing" else []

    async def search(_session, _providers, _query):
        return discovery.SearchResult([Candidate(title=f"{suffix} fixture", doi=doi, pdf_urls=page)], [])

    monkeypatch.setattr(discovery, "search", search)
    added = result(await call("add_paper", identifier=doi))

    assert added == {
        "status": "no_free_pdf",
        "paper_id": None,
        "title": added["title"],
        "landing_url": f"https://doi.org/{discovery_fake.DOI_PREFIX}{suffix}",
    }
    assert jobs.jobs == []


async def test_a_title_is_refused_with_a_hint():
    response = await call("add_paper", identifier="Attention is all you need")
    assert response.is_error and response.structured_content["error"] == "invalid_identifier"


async def test_a_title_with_a_colon_is_not_taken_for_a_link():
    response = await call("add_paper", identifier="BERT: Pre-training of deep bidirectional transformers")
    assert response.is_error and response.structured_content["error"] == "invalid_identifier"


async def test_a_pdf_link_is_downloaded_and_queued(jobs):
    added = result(await call("add_paper", identifier=f"{discovery_fake.PDF_HOST}/paper.pdf"))
    assert added["status"] == "queued" and jobs.jobs == [("ingest_paper", added["paper_id"])]


async def test_a_link_to_an_html_page_is_no_free_pdf(jobs):
    link = f"{discovery_fake.PDF_HOST}/page.html"
    added = result(await call("add_paper", identifier=link))
    assert added["status"] == "no_free_pdf" and added["landing_url"] == link and jobs.jobs == []


@pytest.mark.parametrize(
    "link",
    [
        "file:///etc/passwd",
        "ftp://example.org/a.pdf",
        "http://127.0.0.1/a.pdf",
        "http://10.0.0.1/a.pdf",
        "http://[::1]/a.pdf",
        "http://169.254.169.254/latest",
        "http://[64:ff9b::a00:1]/a.pdf",
        "http://[::127.0.0.1]/a.pdf",
        "http://[fec0::1]/a.pdf",
        "http://[::1/a.pdf",
        "http://example.org:99999/a.pdf",
    ],
)
async def test_unsafe_links_are_refused_before_any_request(link, jobs):
    response = await call("add_paper", identifier=link)
    assert response.is_error and response.structured_content["error"] == "unsafe_url"


async def test_a_host_resolving_to_a_private_address_is_refused(monkeypatch):
    async def private(host):
        return ["192.168.1.20"]

    monkeypatch.setattr(pdf_links, "resolve_host", private)
    response = await call("add_paper", identifier="https://intranet.example/a.pdf")
    assert response.is_error and response.structured_content["error"] == "unsafe_url"


async def test_a_redirect_to_a_private_address_is_refused_and_never_requested(monkeypatch, jobs):
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://10.0.0.5/secret.pdf"})

    @asynccontextmanager
    async def redirecting(_session):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True)
        providers = discovery.Providers(pdf=client)
        try:
            yield providers
        finally:
            await providers.aclose()

    monkeypatch.setattr(discovery, "open_providers", redirecting)
    response = await call("add_paper", identifier="https://pdfs.example.org/a.pdf")

    assert response.is_error and response.structured_content["error"] == "unsafe_url"
    assert asked == ["https://pdfs.example.org/a.pdf"]
    assert jobs.jobs == []


async def test_add_paper_into_a_workspace_files_it(session, jobs):
    ws = await workspaces.create(session, f"Add into {RUN}")
    added = result(await call("add_paper", identifier=FREE_DOI, workspace=f"Add into {RUN}"))
    assert [p.id for p in await workspaces.papers(session, ws.id)] == [uuid.UUID(added["paper_id"])]


async def test_an_unknown_workspace_is_refused_before_any_download(jobs):
    response = await call("add_paper", identifier=FREE_DOI, workspace=f"Nope {RUN}")
    assert response.is_error and response.structured_content["error"] == "unknown_workspace"
    assert jobs.jobs == []


@pytest.mark.parametrize(
    "link",
    [
        "https://link.springer.com/content/pdf/10.1007/s11192-020-03690-4.pdf",
        "https://www.biorxiv.org/content/10.1101/2020.03.22.002386v3.full.pdf",
    ],
)
async def test_a_publisher_link_with_a_doi_in_its_path_is_a_link_not_a_doi(link, jobs):
    added = result(await call("add_paper", identifier=link))
    assert added["status"] == "no_free_pdf" and added["landing_url"] == link and jobs.jobs == []


async def test_a_doi_that_matches_no_search_result_is_no_matching_paper(monkeypatch):
    async def search(_session, _providers, _query):
        return discovery.SearchResult([Candidate(title="Other", doi="10.1234/other")], [])

    monkeypatch.setattr(discovery, "search", search)
    response = await call("add_paper", identifier=FREE_DOI)
    assert response.is_error and response.structured_content["error"] == "no_matching_paper"


async def test_a_failed_paper_already_in_the_library_is_queued_again(session, jobs, monkeypatch, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, title="Failed one")
    await papers.set_status(session, paper.id, PaperStatus.FAILED, "boom")

    async def search(_session, _providers, _query):
        return discovery.SearchResult([Candidate(title="Failed one", doi=FREE_DOI, paper_id=paper.id)], [])

    monkeypatch.setattr(discovery, "search", search)
    added = result(await call("add_paper", identifier=FREE_DOI))

    assert added["status"] == "queued" and jobs.jobs == [("ingest_paper", str(paper.id))]
    assert (await papers.get_paper(session, paper.id)).status == PaperStatus.UPLOADED


async def test_an_empty_workspace_name_is_refused_not_ignored(jobs):
    response = await call("add_paper", identifier=FREE_DOI, workspace="")
    assert response.is_error and response.structured_content["error"] == "unknown_workspace"
    assert jobs.jobs == []


async def test_add_papers_reports_each_item(session, jobs, monkeypatch):
    opened = []
    real = discovery.open_providers
    monkeypatch.setattr(discovery, "open_providers", lambda s: (opened.append(1), real(s))[1])
    ws = await workspaces.create(session, f"Bulk {RUN}")

    nope = f"{discovery_fake.DOI_PREFIX}nope"
    identifiers = [FREE_DOI, "Attention is all you need", f" doi:{FREE_DOI} ", nope]
    items = result(await call("add_papers", identifiers=identifiers, workspace=ws.name))

    assert [(i["identifier"], i["status"], i["error"]) for i in items] == [
        (FREE_DOI, "queued", None),
        ("Attention is all you need", None, "invalid_identifier"),
        (nope, None, "no_matching_paper"),
    ]
    assert len(opened) == 1 and jobs.jobs == [("ingest_paper", items[0]["paper_id"])]
    assert [p.id for p in await workspaces.papers(session, ws.id)] == [uuid.UUID(items[0]["paper_id"])]


async def test_add_papers_unknown_workspace_adds_nothing(jobs):
    response = await call("add_papers", identifiers=[FREE_DOI], workspace=f"Nope {RUN}")
    assert response.is_error and response.structured_content["error"] == "unknown_workspace"
    assert jobs.jobs == []


async def test_add_papers_over_the_cap(jobs):
    response = await call("add_papers", identifiers=[f"10.1/{n}" for n in range(51)])
    assert response.is_error and response.structured_content["error"] == "too_many_identifiers"
    assert response.structured_content["max"] == 50 and jobs.jobs == []


async def test_add_papers_carries_on_after_an_unexpected_failure(session, jobs, monkeypatch):
    real = pipeline._add

    async def flaky(session, providers, kind, value, workspace_uuid):
        if kind == "arxiv":
            raise RuntimeError("provider blew up")
        return await real(session, providers, kind, value, workspace_uuid)

    monkeypatch.setattr(pipeline, "_add", flaky)
    ids = [FREE_DOI, discovery_fake.FREE_ARXIV_ID, f"{discovery_fake.DOI_PREFIX}nope"]

    items = result(await call("add_papers", identifiers=ids))

    outcomes = [(i["status"], i["error"]) for i in items]
    assert outcomes == [("queued", None), (None, "failed"), (None, "no_matching_paper")]
    assert jobs.jobs[0] == ("ingest_paper", items[0]["paper_id"])
