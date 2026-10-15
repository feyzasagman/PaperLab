import pytest
from sqlalchemy import delete, text

from app.core import discovery, references
from app.core.errors import Conflict
from app.core.paper_sources import SOURCES, SourceSettings
from app.models import Paper
from app.providers import (
    acm_dl,
    arxiv,
    core_ac,
    crossref,
    discovery_fake,
    doaj,
    europe_pmc,
    hal,
    openaire,
    openalex,
    pmc,
    pubmed,
    semantic_scholar,
    ssrn,
    zenodo,
)
from app.providers.extraction import extract

# Fetches and embeds take one advisory lock (core/references.py); in a test it lasts until the rollback, so these
# files share one xdist worker, or two of them deadlock on the same stored reference.
pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


@pytest.fixture
async def fake_providers(session):
    # An E2E run killed mid-test can leave the fake's papers in the shared dev database (D15); hide them here.
    await session.execute(delete(Paper).where(Paper.doi.like(f"{discovery_fake.DOI_PREFIX}%")))
    every_source = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCES, True))
    providers = discovery.build_providers(every_source, discovery_fake.transport())
    yield providers
    await providers.aclose()


async def test_the_e2e_fake_finds_three_papers_and_suggests_the_same_three(session, fake_providers):
    found = await discovery.search(session, fake_providers, "anything")
    suggested = await discovery.similar(session, fake_providers, Paper(title="A reader's paper", file_path="/x.pdf"))

    titles = [discovery_fake.FREE_TITLE, discovery_fake.LANDING_TITLE, discovery_fake.CLOSED_TITLE]
    assert ([c.title for c in found.results], found.notices) == (titles, [])
    assert [c.title for c in suggested] == titles
    assert [c.sources for c in found.results] == [
        (
            "openalex", "crossref", "arxiv", "core", "pubmed", "pmc", "europe_pmc", "zenodo", "hal", "acm_dl",
            "ssrn", "doaj", "openaire", "dblp",
        ),
        (
            "openalex", "crossref", "pubmed", "pmc", "europe_pmc", "zenodo", "hal", "acm_dl", "ssrn", "doaj",
            "openaire", "dblp",
        ),
        (
            "openalex", "crossref", "pubmed", "pmc", "europe_pmc", "zenodo", "hal", "acm_dl", "ssrn", "doaj",
            "openaire", "dblp",
        ),
    ]  # fmt: skip
    assert [bool(c.pdf_urls) for c in found.results] == [True, True, False]


async def test_with_openalex_off_as_on_the_e2e_stack_the_badges_are_m19s(session):
    defaults = SourceSettings(contact_email=discovery_fake.MAILTO)
    providers = discovery.build_providers(defaults, discovery_fake.transport())

    found = await discovery.search(session, providers, "anything")
    await providers.aclose()

    assert [c.sources for c in found.results] == [
        ("crossref", "arxiv", "core", "pubmed", "pmc", "europe_pmc", "zenodo", "hal", "acm_dl", "doaj", "openaire"),
        ("crossref", "pubmed", "pmc", "europe_pmc", "zenodo", "hal", "acm_dl", "doaj", "openaire"),
        ("crossref", "pubmed", "pmc", "europe_pmc", "zenodo", "hal", "acm_dl", "doaj", "openaire"),
    ]  # fmt: skip
    assert [bool(c.pdf_urls) for c in found.results] == [True, True, False]  # the landing page's link from Unpaywall


async def test_the_e2e_fakes_free_paper_downloads_a_pdf_extraction_can_title(session, fake_providers, pdf_dir):
    [free, landing, _] = (await discovery.search(session, fake_providers, "anything")).results

    paper = await discovery.add(session, fake_providers, free, pdf_dir)

    assert extract(paper.file_path).title == discovery_fake.FREE_TITLE
    with pytest.raises(Conflict, match="No free PDF was found"):
        await discovery.add(session, fake_providers, landing, pdf_dir)


async def test_the_e2e_fake_answers_identifier_lookups_with_its_free_paper(session, fake_providers):
    by_doi = await discovery.search(session, fake_providers, "10.5555/anything")
    by_arxiv = await discovery.search(session, fake_providers, "2401.00001")

    assert [c.title for c in by_doi.results] == [c.title for c in by_arxiv.results] == [discovery_fake.FREE_TITLE]


async def test_the_e2e_fake_gives_any_paper_three_references_and_one_citing_work(session, fake_providers):
    # Shadow, not delete (same trick as conftest.py's SHADOW_SEARCH_SOURCE) — the owner's links would change
    # nothing here, but a DELETE on a large paper_references table would still take a real row lock per row.
    await session.execute(text("CREATE TEMP TABLE paper_references (LIKE public.paper_references INCLUDING ALL)"))
    reader = Paper(title="A reader's paper", file_path="/x.pdf")
    session.add(reader)
    await session.flush()

    notices = await references.fetch(session, fake_providers, reader)

    cites = await references.listing(session, reader.id, "cites")
    cited_by = await references.listing(session, reader.id, "cited_by")
    assert notices == []
    assert sorted(r.title for r in cites.rows) == sorted(
        [discovery_fake.FREE_TITLE, discovery_fake.LANDING_TITLE, discovery_fake.CLOSED_TITLE]
    )
    assert [r.title for r in cited_by.rows] == [discovery_fake.FREE_TITLE]
    assert {r.title: r.has_pdf for r in cites.rows}[discovery_fake.CLOSED_TITLE] is False


# search_page() pagination (Task 11): a separate 3-item fixture list, distinct from PAPERS, so these don't disturb
# the single-page Find Papers / Similar / references tests above. page_size=2 against 3 items exhausts on page 2
# (a short page) for every provider, including OpenAlex's 1-based `page` cursor.


async def test_the_e2e_fakes_arxiv_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await arxiv.search_page(fake_providers.client("arxiv"), "anything", page_size=2, cursor=0)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await arxiv.search_page(fake_providers.client("arxiv"), "anything", page_size=2, cursor=cursor1)
    assert len(page2) == 1
    assert cursor2 is None
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})


async def test_the_e2e_fakes_arxiv_search_page_id_does_not_collide_with_the_free_papers_id(fake_providers):
    [entry], _ = await arxiv.search_page(fake_providers.client("arxiv"), "anything", page_size=1, cursor=0)
    assert entry["arxiv_id"] != discovery_fake.FREE_ARXIV_ID


async def test_the_e2e_fakes_crossref_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await crossref.search_page(fake_providers.client("crossref"), "anything", page_size=2, cursor=0)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await crossref.search_page(
        fake_providers.client("crossref"), "anything", page_size=2, cursor=cursor1
    )
    assert len(page2) == 1
    assert cursor2 is None
    assert {i["title"][0] for i in page1}.isdisjoint({i["title"][0] for i in page2})


async def test_the_e2e_fakes_core_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await core_ac.search_page(fake_providers.client("core"), "anything", page_size=2, cursor=0)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await core_ac.search_page(fake_providers.client("core"), "anything", page_size=2, cursor=cursor1)
    assert len(page2) == 1
    assert cursor2 is None
    assert {r["title"] for r in page1}.isdisjoint({r["title"] for r in page2})


async def test_the_e2e_fakes_semantic_scholar_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await semantic_scholar.search_page(
        fake_providers.client("semantic_scholar"), "anything", page_size=2, cursor=0
    )
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await semantic_scholar.search_page(
        fake_providers.client("semantic_scholar"), "anything", page_size=2, cursor=cursor1
    )
    assert len(page2) == 1
    assert cursor2 is None
    assert {p["title"] for p in page1}.isdisjoint({p["title"] for p in page2})


async def test_the_e2e_fakes_s2_search_page_ids_do_not_collide_with_the_original_papers_ids(fake_providers):
    page, _ = await semantic_scholar.search_page(
        fake_providers.client("semantic_scholar"), "anything", page_size=3, cursor=0
    )
    original_paper_ids = {f"{i + 1:040x}" for i in range(3)}  # PAPERS' s2_id, via _s2_paper(0|1|2, ...)
    assert {p["paperId"] for p in page}.isdisjoint(original_paper_ids)


async def test_the_e2e_fakes_openalex_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await openalex.search_page(fake_providers.client("openalex"), "anything", page_size=2, cursor=1)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await openalex.search_page(
        fake_providers.client("openalex"), "anything", page_size=2, cursor=cursor1
    )
    assert len(page2) == 1
    assert cursor2 is None
    assert {r["title"] for r in page1}.isdisjoint({r["title"] for r in page2})


async def test_the_e2e_fakes_pubmed_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await pubmed.search_page(fake_providers.client("pubmed"), "anything", page_size=2, cursor=0)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await pubmed.search_page(fake_providers.client("pubmed"), "anything", page_size=2, cursor=cursor1)
    assert len(page2) == 1
    assert cursor2 is None
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})


async def test_the_e2e_fakes_pubmed_search_page_ids_do_not_collide_with_the_free_papers_id(fake_providers):
    [entry], _ = await pubmed.search_page(fake_providers.client("pubmed"), "anything", page_size=1, cursor=0)
    assert entry["pmid"] != discovery_fake.FREE_PUBMED_ID


async def test_the_e2e_fakes_pmc_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await pmc.search_page(fake_providers.client("pmc"), "anything", page_size=2, cursor=0)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await pmc.search_page(fake_providers.client("pmc"), "anything", page_size=2, cursor=cursor1)
    assert len(page2) == 1
    assert cursor2 is None
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})


async def test_the_e2e_fakes_pmc_search_page_ids_do_not_collide_with_the_free_papers_id(fake_providers):
    [entry], _ = await pmc.search_page(fake_providers.client("pmc"), "anything", page_size=1, cursor=0)
    assert entry["pmcid"] != discovery_fake.FREE_PMC_ID


async def test_the_e2e_fakes_europe_pmc_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await europe_pmc.search_page(
        fake_providers.client("europe_pmc"), "anything", page_size=2, cursor="*"
    )
    assert len(page1) == 2
    assert cursor1 == "2"

    page2, cursor2 = await europe_pmc.search_page(
        fake_providers.client("europe_pmc"), "anything", page_size=2, cursor=cursor1
    )
    assert len(page2) == 1
    assert cursor2 is None
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})


async def test_the_e2e_fakes_europe_pmc_search_page_ids_do_not_collide_with_the_free_papers_id(fake_providers):
    [entry], _ = await europe_pmc.search_page(fake_providers.client("europe_pmc"), "anything", page_size=1, cursor="*")
    assert entry["pmid"] != discovery_fake.FREE_EUROPE_PMC_ID


async def test_the_e2e_fakes_zenodo_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await zenodo.search_page(fake_providers.client("zenodo"), "anything", page_size=2, cursor=1)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await zenodo.search_page(fake_providers.client("zenodo"), "anything", page_size=2, cursor=cursor1)
    assert len(page2) == 1
    assert cursor2 is None
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})


async def test_the_e2e_fakes_zenodo_search_page_ids_do_not_collide_with_the_free_papers_id(fake_providers):
    [entry], _ = await zenodo.search_page(fake_providers.client("zenodo"), "anything", page_size=1, cursor=1)
    assert entry["id"] != discovery_fake.FREE_ZENODO_ID


async def test_the_e2e_fakes_hal_search_page_has_two_pages_then_exhausts(fake_providers):
    page1, cursor1 = await hal.search_page(fake_providers.client("hal"), "anything", page_size=2, cursor=0)
    assert len(page1) == 2
    assert cursor1 == 2

    page2, cursor2 = await hal.search_page(fake_providers.client("hal"), "anything", page_size=2, cursor=cursor1)
    assert len(page2) == 1
    assert cursor2 is None
    assert {e["title"] for e in page1}.isdisjoint({e["title"] for e in page2})


async def test_the_e2e_fakes_hal_search_page_ids_do_not_collide_with_the_free_papers_id(fake_providers):
    [entry], _ = await hal.search_page(fake_providers.client("hal"), "anything", page_size=1, cursor=0)
    assert entry["docid"] != discovery_fake.FREE_HAL_ID


async def test_the_e2e_fakes_acm_dl_search_page_has_two_pages_then_exhausts(fake_providers):
    page_1, cursor_1 = await acm_dl.search_page(fake_providers.client("acm_dl"), "x", 2, 0)
    page_2, cursor_2 = await acm_dl.search_page(fake_providers.client("acm_dl"), "x", 2, cursor_1)

    assert len(page_1) == 2
    assert cursor_1 == 2
    assert len(page_2) == 1
    assert cursor_2 is None


async def test_the_e2e_fakes_ssrn_search_page_has_two_pages_then_exhausts(fake_providers):
    page_1, cursor_1 = await ssrn.search_page(fake_providers.client("ssrn"), "x", 2, 0)
    page_2, cursor_2 = await ssrn.search_page(fake_providers.client("ssrn"), "x", 2, cursor_1)

    assert len(page_1) == 2
    assert cursor_1 == 2
    assert len(page_2) == 1
    assert cursor_2 is None


async def test_the_e2e_fakes_doaj_search_page_has_two_pages_then_exhausts(fake_providers):
    page_1, cursor_1 = await doaj.search_page(fake_providers.client("doaj"), "x", 2, 1)
    page_2, cursor_2 = await doaj.search_page(fake_providers.client("doaj"), "x", 2, cursor_1)

    assert len(page_1) == 2
    assert cursor_1 == 2
    assert len(page_2) == 1
    assert cursor_2 is None


async def test_the_e2e_fakes_doaj_search_page_id_does_not_collide_with_the_free_papers_id(fake_providers):
    page_1, _ = await doaj.search_page(fake_providers.client("doaj"), "x", 1, 1)

    assert page_1[0]["id"] != discovery_fake.FREE_DOAJ_ID


async def test_the_e2e_fakes_openaire_search_page_has_two_pages_then_exhausts(fake_providers):
    page_1, cursor_1 = await openaire.search_page(fake_providers.client("openaire"), "x", 2, 1)
    page_2, cursor_2 = await openaire.search_page(fake_providers.client("openaire"), "x", 2, cursor_1)

    assert len(page_1) == 2
    assert cursor_1 == 2
    assert len(page_2) == 1
    assert cursor_2 is None


async def test_the_e2e_fakes_openaire_search_page_id_does_not_collide_with_the_free_papers_id(fake_providers):
    page_1, _ = await openaire.search_page(fake_providers.client("openaire"), "x", 1, 1)

    assert page_1[0]["id"] != discovery_fake.FREE_OPENAIRE_ID
