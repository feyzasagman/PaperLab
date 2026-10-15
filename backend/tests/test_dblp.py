import httpx
import pytest

from app.core.candidates import from_dblp, merge
from app.providers import dblp

pytestmark = pytest.mark.anyio


async def test_author_homonym_suffix_is_removed_and_surname_merge_works():
    from app.core.candidates import Candidate, surname

    candidate = from_dblp({
        "title": "Shared title", "authors": {"author": [{"text": "Wei Wang 0001"}, "Ada Author 123"]},
    })
    assert candidate.authors == ["Wei Wang", "Ada Author 123"]
    assert surname(candidate.authors[0]) == "wang"
    other = Candidate(title="Shared title", authors=["Wei Wang"], sources=("crossref",))
    assert len(merge({"crossref": [other], "dblp": [candidate]}, 10)) == 1


@pytest.mark.parametrize("venue,expected", [(["NeurIPS", "CoRR"], "NeurIPS"), ([], None), ("CoRR", "CoRR")])
async def test_venue_is_a_string_or_none(venue, expected):
    assert from_dblp({"venue": venue}).venue == expected


async def test_title_is_unescaped_and_trailing_period_removed():
    assert from_dblp({"title": "Attention &amp; Learning."}).title == "Attention & Learning"


async def test_corr_arxiv_doi_maps_to_preprint_and_merges_with_conference_version():
    preprint = from_dblp({
        "key": "journals/corr/abs-1706-03762", "title": "Attention is All you Need.",
        "doi": "10.48550/arXiv.1706.03762", "venue": "CoRR",
    })
    assert preprint.external_ids == {"dblp": "journals/corr/abs-1706-03762", "arxiv": "1706.03762"}
    assert preprint.pdf_urls == ["https://arxiv.org/pdf/1706.03762"]
    conference = from_dblp({
        "key": "conf/nips/VaswaniSPUJGKP17", "title": "Attention is All you Need.",
        "ee": "https://arxiv.org/abs/1706.03762", "venue": "NeurIPS",
    })
    assert len(merge({"dblp": [conference, preprint]}, 10)) == 1


@pytest.mark.parametrize("key", ["conf/nips/VaswaniSPUJGKP17", "journals/corr/abs-1706-03762"])
async def test_real_dblp_keys_are_accepted(key):
    from app.schemas.discovery import CandidateIn

    CandidateIn(title="Test", external_ids={"dblp": key})


@pytest.mark.parametrize("key", ["../paper", "conf/../paper", "conf/test?query", "anything", "conf//test"])
async def test_malformed_dblp_keys_are_rejected(key):
    from pydantic import ValidationError

    from app.schemas.discovery import CandidateIn

    with pytest.raises(ValidationError):
        CandidateIn(title="Test", external_ids={"dblp": key})


def body(entries, total=None):
    return {
        "result": {
            "status": {"@code": "200"},
            "hits": {
                "@total": str(len(entries) if total is None else total),
                "@sent": str(len(entries)),
                "hit": [{"info": entry} for entry in entries],
            },
        }
    }


async def test_search_and_pagination():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body([{"key": "conf/test/1", "title": "Test"}], 2))

    async with dblp.new_client(
        email="private@example.org", api_key="secret", transport=httpx.MockTransport(handle)
    ) as http:
        entries, cursor = await dblp.search_page(http, "Test: (Learning)", 1, 0)
        assert entries[0]["key"] == "conf/test/1"
        assert cursor == 1
        _, cursor = await dblp.search_page(http, "Test", 1, 1)
        assert cursor is None
    request = requests[0]
    assert request.url.path == "/search/publ/api"
    assert dict(request.url.params) == {"q": "test learning", "format": "json", "h": "1", "f": "0", "c": "0"}
    assert "secret" not in str(request.headers)
    assert "private@example.org" not in str(request.headers)


async def test_empty_results_and_blank_query():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body([]))

    async with dblp.new_client(transport=httpx.MockTransport(handle)) as http:
        assert await dblp.search(http, "the and", 10) == []
        assert not requests
        assert await dblp.search_page(http, "unknown", 10, 0) == ([], None)


@pytest.mark.parametrize(
    "status,payload",
    [
        (429, b"busy"),
        (500, b"failed"),
        (202, b"<html>Anubis</html>"),
        (200, b"{}"),
        (200, b'{"result":{"status":{"@code":"500"}}}'),
    ],
)
async def test_failure_is_http_error(status, payload):
    async with dblp.new_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, content=payload))
    ) as http:
        with pytest.raises(httpx.HTTPError):
            await dblp.search(http, "test", 10)


async def test_timeout_propagates():
    def handle(request):
        raise httpx.ReadTimeout("timed out", request=request)

    async with dblp.new_client(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(httpx.ReadTimeout):
            await dblp.search(http, "test", 10)


async def test_mapping_metadata_and_safe_pdf_resolution():
    candidate = from_dblp(
        {
            "key": "conf/test/1",
            "title": "Test &amp; Learn",
            "authors": {"author": {"text": "A. Author", "@pid": "1"}},
            "year": "2026",
            "venue": "NeurIPS",
            "doi": "10.1000/TEST",
            "ee": "https://publisher.example/paper.pdf",
        }
    )
    assert candidate.title == "Test & Learn"
    assert candidate.authors == ["A. Author"]
    assert candidate.venue == "NeurIPS" and candidate.year == 2026
    assert candidate.doi == "10.1000/test"
    assert candidate.pdf_urls == []
    assert merge({"dblp": [candidate]}, 10)[0].external_ids == {"dblp": "conf/test/1"}
    preprint = from_dblp({"ee": ["https://arxiv.org/abs/1706.03762v7"], "year": "unknown"})
    assert preprint.pdf_urls == ["https://arxiv.org/pdf/1706.03762"]
    assert preprint.year is None


async def test_opt_in_persists_and_discovery_survives_another_source_failure(session):
    from app.core import discovery, paper_sources
    from app.core.source_registry import SOURCE_IDS
    from app.schemas.discovery import CandidateIn

    settings = await paper_sources.update(session, {"enabled": {s: s in {"dblp", "crossref"} for s in SOURCE_IDS}})
    assert (await paper_sources.get(session)).enabled["dblp"] is True

    def handle(request):
        if request.url.host == "dblp.org":
            return httpx.Response(200, json=body([{"key": "conf/test/1", "title": "Test", "year": "2026"}]))
        return httpx.Response(503)

    providers = discovery.build_providers(settings, httpx.MockTransport(handle))
    try:
        result = await discovery.search(session, providers, "test")
        assert result.results[0].sources == ("dblp",)
        assert result.notices == ["Crossref is busy or unreachable."]
        CandidateIn(title="Test", external_ids=result.results[0].external_ids)
    finally:
        await providers.aclose()


async def test_dblp_failure_is_a_notice_or_conflict_when_all_fail(session):
    from app.core import discovery
    from app.core.errors import Conflict
    from app.core.paper_sources import SourceSettings
    from app.core.source_registry import SOURCE_IDS

    def handle(request):
        if request.url.host == "dblp.org":
            return httpx.Response(202, text="<html>Anubis</html>")
        return httpx.Response(200, json={"message": {"items": []}})

    settings = SourceSettings(enabled={s: s in {"dblp", "crossref"} for s in SOURCE_IDS})
    providers = discovery.build_providers(settings, httpx.MockTransport(handle))
    try:
        result = await discovery.search(session, providers, "test")
        assert result.results == []
        assert result.notices == ["DBLP is busy or unreachable."]
        with pytest.raises(Conflict, match="DBLP is busy or unreachable"):
            await discovery.search(session, providers.without("crossref"), "test")
    finally:
        await providers.aclose()


async def test_default_settings_never_contact_dblp():
    from app.core.discovery import build_providers
    from app.core.paper_sources import SourceSettings

    providers = build_providers(SourceSettings())
    try:
        assert providers.client("dblp") is None
    finally:
        await providers.aclose()
