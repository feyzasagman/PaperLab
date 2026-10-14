"""M39 F4 MCP tools: verify_claim and verify_claims."""

import json
import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result

from app.core import claims, screening, workspaces
from app.models import Paper, PaperStatus
from mcp_server import server

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]
RUN = uuid.uuid4().hex[:8]
ABSTRACT = "We study how large language models homogenize creative writing. Outputs were 23% less diverse."


class Scripted:
    model, connection_name = "fake:scripted", "Test"

    def __init__(self):
        self.calls = 0

    async def stream(self, system, prompt):
        self.calls += 1
        yield json.dumps({"verdict": "not_addressed", "passage": None, "quote": "", "reason": "Not covered."})


@pytest.fixture
def llm(monkeypatch):
    fake = Scripted()

    async def given(_session, _llm):
        return fake

    monkeypatch.setattr(claims, "_llm", given)
    return fake


def _model(monkeypatch, local: bool):
    async def default(_session):
        return ("Test · model", local, "localhost" if local else "api.example.com")

    monkeypatch.setattr(screening, "_default_model", default)


async def paper(session, doi=None, abstract=ABSTRACT, title="Homogenized writing", authors=None, year=None):
    row = Paper(
        title=title, file_path="x.pdf", status=PaperStatus.READY, abstract=abstract, doi=doi,
        authors=authors or [], year=year,
    )
    session.add(row)
    await session.flush()
    return row


async def test_verify_claim_returns_the_check_and_saves_nothing_by_default(session, llm, monkeypatch):
    _model(monkeypatch, True)
    row = await paper(session)
    check = result(await call("verify_claim", claim="LLMs reduce diversity.", paper_id=str(row.id)))
    assert check["verdict"] == "not_addressed" and check["evidence_level"] == "abstract_only"
    assert check["paper_id"] == str(row.id) and check["note_id"] is None and llm.calls == 1


async def test_verify_claim_by_doi_and_metadata_only_asks_no_model(session, llm, monkeypatch):
    _model(monkeypatch, True)
    doi = f"10.9999/bare.{RUN}"
    await paper(session, doi=doi, abstract=None)
    check = result(await call("verify_claim", claim="Anything.", doi=doi))
    assert (check["verdict"], check["reason"]) == ("cannot_verify", "no_text") and llm.calls == 0


async def test_a_remote_model_is_refused_until_allow_remote(session, llm, monkeypatch):
    _model(monkeypatch, False)
    row = await paper(session)
    refused = await call("verify_claim", claim="LLMs reduce diversity.", paper_id=str(row.id))
    assert refused.structured_content["error"] == "remote_model_needs_confirm"
    assert refused.structured_content["model"] == "Test · model" and llm.calls == 0
    ok = result(await call("verify_claim", claim="LLMs reduce diversity.", paper_id=str(row.id), allow_remote=True))
    assert ok["verdict"] == "not_addressed" and llm.calls == 1


async def test_verify_claim_errors(session, llm, monkeypatch):
    _model(monkeypatch, True)
    row = await paper(session)
    both = await call("verify_claim", claim="x y z.", paper_id=str(row.id), doi="10.1/x")
    assert both.structured_content["error"] == "claim_scope"
    gone = await call("verify_claim", claim="x y z.", doi=f"10.9999/none.{RUN}")
    assert gone.structured_content["error"] == "paper_not_in_library"


async def test_verify_claims_checks_each_citation_and_reports_unmatched_ones(session, llm, monkeypatch):
    _model(monkeypatch, True)
    doi = f"10.9999/cited.{RUN}"
    await paper(session, doi=doi)
    text = f"Models flatten style (doi:{doi}). Nobody knows (doi:10.9999/missing.{RUN})."
    items = result(await call("verify_claims", text=text))
    assert [i["error"] for i in items] == [None, "citation_not_found"]
    assert items[0]["check"]["verdict"] == "not_addressed"


async def test_verify_claims_returns_not_a_claim_and_year_mismatch(session, llm, monkeypatch):
    _model(monkeypatch, True)
    await paper(session, title=f"Mcp off by one {RUN}", authors=["Ann Mcpoffbyone"], year=1987)
    text = "It rose for every group (Mcpoffbyone, 1988). Lee & Kim (1987) is covered in [[x]]."
    items = result(await call("verify_claims", text=text))
    assert [(i["warning"], i["error"]) for i in items] == [("year_mismatch", None), (None, "not_a_claim")]
    assert "not_a_claim" in server.verify_claims.__doc__ and "reference list" in server.verify_claims.__doc__


async def test_verify_claims_workspace_remote_and_limits(session, llm, monkeypatch):
    _model(monkeypatch, False)
    await workspaces.create(session, f"Claims {RUN}")
    text = "Models flatten style (doi:10.9999/x)."
    refused = await call("verify_claims", text=text, workspace=f"Claims {RUN}")
    assert refused.structured_content["error"] == "remote_model_needs_confirm" and llm.calls == 0
    unknown = await call("verify_claims", text=text, workspace=f"Nope {RUN}", allow_remote=True)
    assert unknown.structured_content["error"] == "unknown_workspace"
    many = " ".join(f"Claim {n} here (doi:10.9999/m{n})." for n in range(31))
    big = await call("verify_claims", text=many, allow_remote=True)
    assert big.structured_content["error"] == "too_many_claims" and llm.calls == 0


async def test_an_unreachable_model_is_model_unavailable(session, monkeypatch):
    from app.providers.base import LLMUnavailable

    _model(monkeypatch, True)

    class Down(Scripted):
        async def stream(self, system, prompt):
            raise LLMUnavailable("down")
            yield

    async def given(_session, _llm):
        return Down()

    monkeypatch.setattr(claims, "_llm", given)
    row = await paper(session)
    out = await call("verify_claim", claim="LLMs reduce diversity.", paper_id=str(row.id))
    assert out.is_error and out.structured_content["error"] == "model_unavailable"
