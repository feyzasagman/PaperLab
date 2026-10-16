"""M39 F1 MCP tools: search runs, hits, snowball, eligibility, PRISMA and notes."""

import dataclasses
import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from pdf_papers import chunked_paper

from app.core import discovery, paper_sources, workspace_search, workspaces
from app.core import notes as notes_core
from app.core.paper_sources import SourceSettings
from app.core.source_registry import SOURCE_IDS
from app.models import Note
from app.models.note import Provenance
from app.providers import discovery_fake

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]
RUN = uuid.uuid4().hex[:8]


async def new_workspace(session, label: str):
    return await workspaces.create(session, f"{label} {RUN}")


async def test_start_search_run_enqueues_once_and_reports_its_run(session, jobs):
    await new_workspace(session, "Runs")
    run = result(
        await call("start_search_run", workspace=f"Runs {RUN}", query="llm creativity", sources=["arxiv", "crossref"])
    )

    # Stored in trust order (D73), not the order asked for.
    assert run["status"] == "running" and sorted(run["sources_json"]) == ["arxiv", "crossref"]
    assert jobs.jobs == [("run_workspace_search", run["id"])]
    assert result(await call("get_search_run", run_id=run["id"]))["id"] == run["id"]


async def test_start_search_run_defaults_to_the_sources_turned_on(session, jobs):
    await new_workspace(session, "Defaults")
    on = await paper_sources.get(session)
    run = result(await call("start_search_run", workspace=f"Defaults {RUN}", query="q"))
    assert run["sources_json"] and all(on.enabled[s] for s in run["sources_json"])


async def test_start_search_run_with_no_search_source_on_is_invalid(session, monkeypatch):
    await new_workspace(session, "None on")
    real = paper_sources.get

    async def all_off(s):
        sources = await real(s)
        return dataclasses.replace(sources, enabled=dict.fromkeys(sources.enabled, False))

    monkeypatch.setattr(paper_sources, "get", all_off)
    response = await call("start_search_run", workspace=f"None on {RUN}", query="q")
    assert response.is_error and response.structured_content["error"] == "invalid_input"


async def test_an_unknown_source_names_the_valid_ones(session):
    await new_workspace(session, "Bad source")
    response = await call("start_search_run", workspace=f"Bad source {RUN}", query="q", sources=["google_scholar"])
    assert response.is_error and response.structured_content["error"] == "unknown_source"
    assert "arxiv" in response.structured_content["valid"]


async def test_a_double_stop_is_a_readable_conflict(session):
    await new_workspace(session, "Twice")
    run = result(await call("start_search_run", workspace=f"Twice {RUN}", query="q", sources=["arxiv"]))
    stopped = result(await call("stop_search_run", run_id=run["id"]))
    stop_again = await call("stop_search_run", run_id=run["id"])

    assert stopped["status"] == "stopped"
    assert stop_again.is_error and stop_again.structured_content["error"] == "search_run_not_running"


async def test_an_unknown_run_is_not_found():
    response = await call("get_search_run", run_id=str(uuid.uuid4()))
    assert response.is_error and response.structured_content["error"] == "not_found"


async def run_with_hits(session, label: str, query: str = "bert"):
    """A workspace with one finished batch of hits from the offline fake sources (arXiv + Semantic Scholar)."""
    ws = await new_workspace(session, label)
    run = await workspace_search.start_run(session, ws.id, query, {}, ["arxiv", "semantic_scholar"])
    every = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCE_IDS, True))
    providers = discovery.build_providers(every, discovery_fake.transport())
    try:
        await workspace_search.search_batch(session, providers, run)
    finally:
        await providers.aclose()
    return ws, run


async def test_list_hits_walks_one_run_with_a_cursor(session):
    ws, run = await run_with_hits(session, "Walk")
    other = await workspace_search.start_run(session, ws.id, "other", {}, ["arxiv"])  # a second run: no hits yet
    seen, cursor = [], None
    while True:
        page = result(await call("list_hits", workspace=f"Walk {RUN}", run_id=str(run.id), cursor=cursor, limit=1))
        seen += [h["id"] for h in page["hits"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen and len(seen) == len(set(seen))
    assert all(h["run_id"] == str(run.id) for h in result(await call("list_hits", workspace=f"Walk {RUN}"))["hits"])
    assert result(await call("list_hits", workspace=f"Walk {RUN}", run_id=str(other.id)))["hits"] == []


async def test_review_hits_then_filter_undecided(session):
    ws, run = await run_with_hits(session, "Review")
    hits = result(await call("list_hits", workspace=f"Review {RUN}"))["hits"]
    first, rest = hits[0]["id"], [h["id"] for h in hits[1:]]

    reviewed = result(await call("review_hits", workspace=f"Review {RUN}", hit_ids=[first], decision="relevant"))
    undecided = result(await call("list_hits", workspace=f"Review {RUN}", status="undecided"))["hits"]

    assert reviewed == {"updated": 1, "skipped": []}
    assert sorted(h["id"] for h in undecided) == sorted(rest)


async def test_not_relevant_needs_a_reason(session):
    await run_with_hits(session, "Reason")
    hit = result(await call("list_hits", workspace=f"Reason {RUN}"))["hits"][0]["id"]
    response = await call("review_hits", workspace=f"Reason {RUN}", hit_ids=[hit], decision="not_relevant")
    assert response.is_error and response.structured_content["error"] == "reason_required"


async def test_a_bad_decision_or_reason_is_refused(session):
    await run_with_hits(session, "Bad")
    hit = result(await call("list_hits", workspace=f"Bad {RUN}"))["hits"][0]["id"]
    bad = await call("review_hits", workspace=f"Bad {RUN}", hit_ids=[hit], decision="include")
    bad_reason = await call(
        "review_hits", workspace=f"Bad {RUN}", hit_ids=[hit], decision="not_relevant", reason="boring"
    )
    assert bad.structured_content["error"] == "invalid_decision"
    assert bad_reason.structured_content["error"] == "invalid_decision"


async def test_foreign_and_unknown_hits_are_skipped_not_written(session):
    await run_with_hits(session, "Mine")
    await run_with_hits(session, "Theirs")
    mine = result(await call("list_hits", workspace=f"Mine {RUN}"))["hits"][0]["id"]
    theirs = result(await call("list_hits", workspace=f"Theirs {RUN}"))["hits"][0]["id"]
    ghost = str(uuid.uuid4())

    reviewed = result(
        await call("review_hits", workspace=f"Mine {RUN}", hit_ids=[mine, theirs, ghost], decision="maybe")
    )

    assert reviewed["updated"] == 1 and sorted(reviewed["skipped"]) == sorted([theirs, ghost])
    after = result(await call("list_hits", workspace=f"Theirs {RUN}"))["hits"]
    assert next(h for h in after if h["id"] == theirs)["stage1_status"] is None


async def test_import_hit_queues_ingest_for_a_free_pdf_and_not_for_a_failure(session, jobs):
    ws, _ = await run_with_hits(session, "Import")
    # The offline search routes only serve the pagination fixtures; the free/closed papers come from a snowball hop
    # (Semantic Scholar's references route), which lands them in the same hit pool.
    seed = result(await call("list_hits", workspace=f"Import {RUN}"))["hits"][0]["id"]
    every = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCE_IDS, True))
    providers = discovery.build_providers(every, discovery_fake.transport())
    try:
        await workspace_search.snowball(session, providers, ws.id, [], [uuid.UUID(seed)], backward=True, forward=False)
    finally:
        await providers.aclose()
    hits = result(await call("list_hits", workspace=f"Import {RUN}"))["hits"]
    free = next(h for h in hits if h["doi"] == f"{discovery_fake.DOI_PREFIX}free")
    closed = next(h for h in hits if h["doi"] == f"{discovery_fake.DOI_PREFIX}closed")

    ok = result(await call("import_hit", workspace=f"Import {RUN}", hit_id=free["id"]))
    failed = result(await call("import_hit", workspace=f"Import {RUN}", hit_id=closed["id"]))

    assert ok["imported"] == 1 and jobs.jobs == [("ingest_paper", ok["paper_ids"][0])]
    assert failed == {"imported": 0, "failed": 1, "paper_ids": []}


async def test_import_of_an_unknown_hit_is_not_found(session):
    await new_workspace(session, "Ghost")
    response = await call("import_hit", workspace=f"Ghost {RUN}", hit_id=str(uuid.uuid4()))
    assert response.is_error and response.structured_content["error"] == "hit_not_found"


async def test_snowball_from_a_library_paper_adds_hits(session, tmp_path):
    ws = await new_workspace(session, "Snow")
    paper, _, _ = await chunked_paper(session, tmp_path, title="Seed paper")
    paper.doi = f"{discovery_fake.DOI_PREFIX}free"
    await session.commit()
    await workspaces.add_paper(session, ws.id, paper.id)

    snow = result(await call("snowball", workspace=f"Snow {RUN}", seed_paper_ids=[str(paper.id)], direction="both"))

    assert set(snow) == {"new_hits", "skipped_seeds", "errors"}
    assert snow["new_hits"] >= 1
    hits = result(await call("list_hits", workspace=f"Snow {RUN}"))["hits"]
    assert hits and {h["source_method"] for h in hits} >= {"snowball_backward"}


async def test_snowball_backward_tags_hits_backward_only(session, tmp_path):
    ws = await new_workspace(session, "Back")
    paper, _, _ = await chunked_paper(session, tmp_path, title="Seed paper")
    paper.doi = f"{discovery_fake.DOI_PREFIX}free"
    await session.commit()
    await workspaces.add_paper(session, ws.id, paper.id)

    snow = result(await call("snowball", workspace=f"Back {RUN}", seed_paper_ids=[str(paper.id)], direction="backward"))

    assert snow["new_hits"] >= 1
    hits = result(await call("list_hits", workspace=f"Back {RUN}"))["hits"]
    assert {h["source_method"] for h in hits} == {"snowball_backward"}


async def test_snowball_with_no_seeds_is_refused_and_makes_no_run(session):
    await new_workspace(session, "Empty")
    response = await call("snowball", workspace=f"Empty {RUN}", seed_paper_ids=[], direction="both")
    assert response.is_error and response.structured_content["error"] == "invalid_input"


async def test_review_reason_is_kept_only_for_not_relevant(session):
    await run_with_hits(session, "Keep")
    hit = result(await call("list_hits", workspace=f"Keep {RUN}"))["hits"][0]["id"]
    await call("review_hits", workspace=f"Keep {RUN}", hit_ids=[hit], decision="maybe", reason="other")
    after = result(await call("list_hits", workspace=f"Keep {RUN}"))["hits"]
    assert next(h for h in after if h["id"] == hit)["stage1_exclude_reason"] is None


async def test_snowball_direction_is_checked(session):
    await new_workspace(session, "Dir")
    response = await call("snowball", workspace=f"Dir {RUN}", seed_paper_ids=[str(uuid.uuid4())], direction="up")
    assert response.is_error and response.structured_content["error"] == "invalid_input"


async def test_eligibility_then_prisma_counts_it(session, tmp_path):
    ws, run = await run_with_hits(session, "Elig")
    paper, _, _ = await chunked_paper(session, tmp_path, title="Full text read")
    await workspaces.add_paper(session, ws.id, paper.id)
    # PRISMA's stage 2 only counts papers behind a hit the owner marked relevant.
    hit = result(await call("list_hits", workspace=f"Elig {RUN}"))["hits"][0]
    await call("review_hits", workspace=f"Elig {RUN}", hit_ids=[hit["id"]], decision="relevant")
    row = await session.get(workspace_search.WorkspaceSearchHit, uuid.UUID(hit["id"]))
    row.paper_id = paper.id
    await session.commit()

    verdict = result(await call(
        "set_eligibility", workspace=f"Elig {RUN}", paper_id=str(paper.id), run_id=str(run.id), verdict="exclude",
        reason="no outcome measure",
    ))
    prisma = result(await call("get_prisma", workspace=f"Elig {RUN}", run_id=str(run.id)))
    combined = result(await call("get_prisma", workspace=f"Elig {RUN}"))

    assert verdict["verdict"] == "exclude" and verdict["reason"] == "no outcome measure"
    assert prisma["stage2_assessed"] == 1 and prisma["stage2_excluded_by_reason"] == {"no outcome measure": 1}
    assert combined["stage2_excluded"] == 1


async def test_a_bad_verdict_and_an_unknown_run_are_readable(session, tmp_path):
    ws = await new_workspace(session, "Bad elig")
    paper, _, _ = await chunked_paper(session, tmp_path, title="P")
    await workspaces.add_paper(session, ws.id, paper.id)
    args = {"workspace": f"Bad elig {RUN}", "paper_id": str(paper.id), "run_id": str(uuid.uuid4())}
    bad = await call("set_eligibility", verdict="maybe", **args)
    ghost = await call("set_eligibility", verdict="include", **args)
    assert bad.structured_content["error"] == "invalid_decision"
    assert ghost.structured_content["error"] == "not_found"


async def notes_on(session, tmp_path, label):
    ws = await new_workspace(session, label)
    paper, _, _ = await chunked_paper(session, tmp_path, title=f"{label} paper")
    await workspaces.add_paper(session, ws.id, paper.id)
    made = {}
    for provenance in Provenance:
        note = Note(body=f"{provenance} note", provenance=provenance)
        session.add(note)
        await session.flush()
        await notes_core.set_papers(session, note.id, [paper.id])
        made[provenance] = note.id
    return ws, paper, made


@pytest.mark.parametrize(
    "author,expected", [("you", {"human"}), ("ai", {"llm", "llm_edited"}), ("all", {"human", "llm", "llm_edited"})]
)
async def test_list_notes_filters_by_author_and_labels_them(session, tmp_path, author, expected):
    _, paper, _ = await notes_on(session, tmp_path, f"Notes {author}")
    rows = result(await call("list_notes", paper_id=str(paper.id), author=author))
    assert {r["provenance"] for r in rows} == expected
    assert {r["provenance"]: r["author"] for r in rows}.items() <= {
        "human": "You", "llm": "AI", "llm_edited": "AI · edited"
    }.items()


async def test_list_notes_by_workspace(session, tmp_path):
    _, _, made = await notes_on(session, tmp_path, "Notes ws")
    rows = result(await call("list_notes", workspace=f"Notes ws {RUN}"))
    assert {r["id"] for r in rows} == {str(i) for i in made.values()}


@pytest.mark.parametrize("args", [{}, {"workspace": "x", "paper_id": str(uuid.uuid4())}])
async def test_list_notes_needs_exactly_one_scope(args):
    response = await call("list_notes", **args)
    assert response.is_error and response.structured_content["error"] == "notes_scope"


async def test_start_search_run_with_a_year_range_stores_it(session, jobs):
    await new_workspace(session, "Years")
    run = result(await call("start_search_run", workspace=f"Years {RUN}", query="q", sources=["arxiv"],
                            year_from=2023, year_to=2026))
    assert run["filters_json"] == {"year_from": 2023, "year_to": 2026}


async def test_a_backwards_year_range_is_invalid_years(session):
    await new_workspace(session, "Bad years")
    response = await call("start_search_run", workspace=f"Bad years {RUN}", query="q", sources=["arxiv"],
                          year_from=2026, year_to=2023)
    assert response.is_error and response.structured_content["error"] == "invalid_years"


async def test_a_weekly_saved_search_is_created_listed_and_paused(session):
    await new_workspace(session, "Sched")
    ws = f"Sched {RUN}"
    made = result(await call(
        "create_saved_search", workspace=ws, query="llm creativity", schedule="weekly", time="06:30", weekday=1,
        timezone="Europe/Berlin",
    ))
    assert made["schedule_kind"] == "weekly" and made["next_run_at"] and made["enabled"]

    assert [s["id"] for s in result(await call("list_saved_searches", workspace=ws))] == [made["id"]]

    paused = result(await call("set_saved_search_enabled", saved_search_id=made["id"], enabled=False))
    assert paused["enabled"] is False and paused["next_run_at"] is None


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"schedule": "hourly"}, "invalid_schedule"),
        ({"cron": "0 3 * * *"}, "invalid_schedule"),  # cron given but schedule stays daily
        ({"weekday": 2}, "invalid_schedule"),  # weekday given but schedule stays daily
        ({"timezone": "Nowhere/X"}, "invalid_timezone"),
        ({"workspace": "No such workspace"}, "unknown_workspace"),
    ],
)
async def test_create_saved_search_errors_are_readable(session, args, code):
    await new_workspace(session, "Bad sched")
    response = await call("create_saved_search", **{"workspace": f"Bad sched {RUN}", "query": "q", **args})
    assert response.is_error and response.structured_content["error"] == code


async def test_pausing_an_unknown_saved_search_says_so(session):
    response = await call("set_saved_search_enabled", saved_search_id=str(uuid.uuid4()), enabled=True)
    assert response.is_error and response.structured_content["error"] == "saved_search_not_found"


# --- AI pre-screening tools ---

from screening_pool import make_pool  # noqa: E402

from app.core import pipeline, screening  # noqa: E402


def _model(monkeypatch, local: bool):
    async def default(_session):
        return ("Test · model", local, "localhost" if local else "api.example.com")

    monkeypatch.setattr(screening, "_default_model", default)


async def _pool(session, criteria="adults"):
    workspace, hits = await make_pool(session, [("a", None)] * 3)
    workspace.inclusion_criteria = criteria
    await session.flush()
    return workspace, hits


async def test_run_prescreen_starts_and_enqueues_with_a_local_model(session, jobs, monkeypatch):
    _model(monkeypatch, True)
    workspace, _ = await _pool(session)
    state = result(await call("run_prescreen", workspace=workspace.name))
    assert (state["suggest_status"], state["suggest_total"]) == ("running", 3)
    assert jobs.jobs == [("suggest_screening", str(workspace.id))]


async def test_stop_prescreen_stops_a_running_prescreen(session, jobs, monkeypatch):
    _model(monkeypatch, True)
    workspace, _ = await _pool(session)
    result(await call("run_prescreen", workspace=workspace.name))
    assert result(await call("stop_prescreen", workspace=workspace.name))["suggest_status"] == "stopping"


async def test_stop_prescreen_while_idle_is_a_no_op(session):
    workspace, _ = await _pool(session)
    assert result(await call("stop_prescreen", workspace=workspace.name))["suggest_status"] == "idle"


async def test_run_prescreen_with_a_remote_model_asks_and_starts_nothing(session, jobs, monkeypatch):
    _model(monkeypatch, False)
    workspace, _ = await _pool(session)
    response = await call("run_prescreen", workspace=workspace.name)
    assert response.is_error and response.structured_content["error"] == "remote_model_needs_confirm"
    assert response.structured_content["model"] == "Test · model"
    assert response.structured_content["host"] == "api.example.com"
    assert jobs.jobs == [] and workspace.suggest_status == "idle"


async def test_run_prescreen_errors(session, jobs, monkeypatch):
    _model(monkeypatch, True)
    workspace, _ = await _pool(session, criteria=None)
    no_criteria = await call("run_prescreen", workspace=workspace.name)
    assert no_criteria.structured_content["error"] == "no_criteria"
    assert (await call("run_prescreen", workspace="Nope " + RUN)).structured_content["error"] == "unknown_workspace"
    _, (other_hit, *_) = await _pool(session)
    workspace.inclusion_criteria = "adults"
    foreign = await call("run_prescreen", workspace=workspace.name, run_id=str(other_hit.run_id))
    assert foreign.is_error and foreign.structured_content["error"] == "not_found"
    assert jobs.jobs == []


async def test_get_screening_suggestions_lists_undecided_suggested_hits_best_first(session):
    workspace, (low, high, none) = await _pool(session)
    for hit, score in ((low, 2), (high, 5)):
        hit.suggestion, hit.suggestion_relevance = "include", score
    await session.flush()

    page = result(await call("get_screening_suggestions", workspace=workspace.name))
    assert [h["id"] for h in page["hits"]] == [str(high.id), str(low.id)]
    only_high = result(await call("get_screening_suggestions", workspace=workspace.name, min_relevance=4))
    assert [h["id"] for h in only_high["hits"]] == [str(high.id)]
    high.stage1_status = "relevant"
    await session.flush()
    again = result(await call("get_screening_suggestions", workspace=workspace.name))
    assert [h["id"] for h in again["hits"]] == [str(low.id)]


async def test_accept_suggestions_records_decisions_and_skips_the_rest(session):
    workspace, (inc, exc, none) = await _pool(session)
    inc.suggestion = "include"
    exc.suggestion, exc.suggestion_reason = "exclude", "wrong_topic"
    await session.flush()
    out = result(await call("accept_suggestions", workspace=workspace.name, hit_ids=[str(inc.id), str(none.id)]))
    assert out == {"accepted": 1, "skipped": [str(none.id)]}
    assert (inc.stage1_status, exc.stage1_status) == ("relevant", None)


async def test_accept_suggestions_refuses_more_than_5000_ids(session):
    workspace, _ = await _pool(session)
    out = await call("accept_suggestions", workspace=workspace.name, hit_ids=[str(uuid.uuid4()) for _ in range(5001)])
    assert out.is_error and out.structured_content["error"] == "too_many_hits"


async def test_get_search_run_and_get_prisma_carry_a_cut_short_run(session, jobs):  # D266
    await new_workspace(session, "Cut short")
    started = result(await call("start_search_run", workspace=f"Cut short {RUN}", query="q", sources=["arxiv"]))
    run = await session.get(workspace_search.WorkspaceSearchRun, uuid.UUID(started["id"]))
    run.status, run.stats_json = "exhausted", {"cut_short": True, "unfinished_sources": ["arxiv"]}
    await session.commit()

    seen = result(await call("get_search_run", run_id=started["id"]))
    assert seen["stats_json"]["cut_short"] is True and seen["stats_json"]["unfinished_sources"] == ["arxiv"]
    prisma = result(await call("get_prisma", workspace=f"Cut short {RUN}"))
    assert len(prisma["notes"]) == 1 and "partial" in prisma["notes"][0]


async def test_get_prisma_names_a_source_that_did_not_answer(session, jobs):
    await new_workspace(session, "Failed src")
    started = result(await call("start_search_run", workspace=f"Failed src {RUN}", query="q", sources=["arxiv"]))
    run = await session.get(workspace_search.WorkspaceSearchRun, uuid.UUID(started["id"]))
    run.status, run.stats_json = "exhausted", {"failed_sources": ["semantic_scholar"]}
    await session.commit()
    notes = result(await call("get_prisma", workspace=f"Failed src {RUN}"))["notes"]
    assert notes == ["Semantic Scholar did not answer; identified excludes it."]

    run.stats_json = {}
    await session.commit()
    assert result(await call("get_prisma", workspace=f"Failed src {RUN}"))["notes"] == []


# --- screening criteria tools (M49) ---


async def _criteria_pool(session):
    workspace, hits = await make_pool(session, [("a", None)])
    workspace.research_question, workspace.inclusion_criteria, workspace.exclusion_criteria = "RQ", "In", "Out"
    hits[0].suggestion = "include"
    await session.flush()
    return workspace, hits[0]


def _criteria(state):
    return state["research_question"], state["inclusion_criteria"], state["exclusion_criteria"]


async def test_get_screening_criteria_reads_the_question_and_criteria(session):
    workspace, _ = await _criteria_pool(session)
    state = result(await call("get_screening_criteria", workspace=workspace.name))
    assert _criteria(state) == ("RQ", "In", "Out")


async def test_set_screening_criteria_changes_only_the_given_fields_and_clears_suggestions(session):
    workspace, hit = await _criteria_pool(session)
    state = result(await call("set_screening_criteria", workspace=workspace.name, exclusion_criteria="New out"))
    assert _criteria(state) == ("RQ", "In", "New out")
    await session.refresh(hit)
    assert hit.suggestion is None


async def test_setting_the_same_values_keeps_the_suggestions(session):
    workspace, hit = await _criteria_pool(session)
    result(await call("set_screening_criteria", workspace=workspace.name, inclusion_criteria="In"))
    await session.refresh(hit)
    assert hit.suggestion == "include"


async def test_an_empty_string_clears_a_criterion_over_mcp(session):
    workspace, hit = await _criteria_pool(session)
    assert workspace.exclusion_criteria == "Out"
    state = result(await call("set_screening_criteria", workspace=workspace.name, exclusion_criteria=""))
    assert state["exclusion_criteria"] is None
    assert _criteria(state)[:2] == ("RQ", "In")
    await session.refresh(hit)
    assert hit.suggestion is None


async def test_set_is_refused_while_suggestions_run(session):
    workspace, _ = await _criteria_pool(session)
    workspace.suggest_status = "running"
    await session.flush()
    response = await call("set_screening_criteria", workspace=workspace.name, inclusion_criteria="x")
    assert response.is_error and response.structured_content["error"] == "suggestions_running"
    assert workspace.inclusion_criteria == "In"


async def test_a_field_over_4000_characters_is_refused(session):
    workspace, _ = await _criteria_pool(session)
    response = await call("set_screening_criteria", workspace=workspace.name, research_question="x" * 4001)
    assert response.is_error and workspace.research_question == "RQ"


async def test_an_unknown_workspace_is_unknown_workspace_for_criteria():
    response = await call("get_screening_criteria", workspace="No such workspace")
    assert response.is_error and response.structured_content["error"] == "unknown_workspace"


async def test_list_search_runs_newest_first_with_hit_counts_and_only_this_workspace(session, jobs):
    await new_workspace(session, "Many runs")
    await new_workspace(session, "Other runs")
    first = result(await call("start_search_run", workspace=f"Many runs {RUN}", query="one", sources=["arxiv"]))
    second = result(await call("start_search_run", workspace=f"Many runs {RUN}", query="two", sources=["arxiv"]))
    result(await call("start_search_run", workspace=f"Other runs {RUN}", query="foreign", sources=["arxiv"]))
    runs = result(await call("list_search_runs", workspace=f"Many runs {RUN}"))
    assert [r["id"] for r in runs] == [second["id"], first["id"]]
    assert runs[0]["query_text"] == "two" and runs[0]["hit_count"] == 0 and runs[0]["sources_json"] == ["arxiv"]
    assert "stats_json" not in runs[0]


async def test_list_search_runs_of_an_empty_workspace_is_empty(session):
    await new_workspace(session, "No runs")
    assert result(await call("list_search_runs", workspace=f"No runs {RUN}")) == []


async def test_list_search_runs_clamps_the_limit(session, jobs):
    await new_workspace(session, "Clamp runs")
    ws = f"Clamp runs {RUN}"
    for query in ("one", "two"):
        result(await call("start_search_run", workspace=ws, query=query, sources=["arxiv"]))
    assert len(result(await call("list_search_runs", workspace=ws, limit=-1))) == 1
    assert len(result(await call("list_search_runs", workspace=ws, limit=1))) == 1


async def test_run_prescreen_without_a_run_refuses_a_big_queue_unless_confirmed(session, jobs, monkeypatch):
    _model(monkeypatch, True)
    monkeypatch.setattr(pipeline, "PRESCREEN_CONFIRM_ABOVE", 2)
    workspace, _ = await _pool(session)  # 3 hits queued
    refused = await call("run_prescreen", workspace=workspace.name)
    assert refused.is_error and refused.structured_content["error"] == "prescreen_needs_confirm"
    assert (refused.structured_content["queued"], refused.structured_content["limit"]) == (3, 2)
    assert jobs.jobs == [] and workspace.suggest_status == "idle"
    assert result(await call("run_prescreen", workspace=workspace.name, confirm=True))["suggest_status"] == "running"


async def test_run_prescreen_at_the_limit_starts_without_confirm(session, jobs, monkeypatch):
    _model(monkeypatch, True)
    monkeypatch.setattr(pipeline, "PRESCREEN_CONFIRM_ABOVE", 3)
    workspace, _ = await _pool(session)
    assert result(await call("run_prescreen", workspace=workspace.name))["suggest_status"] == "running"


async def test_run_prescreen_for_one_run_never_needs_confirm(session, jobs, monkeypatch):
    _model(monkeypatch, True)
    monkeypatch.setattr(pipeline, "PRESCREEN_CONFIRM_ABOVE", 0)
    workspace, (hit, *_) = await _pool(session)
    state = result(await call("run_prescreen", workspace=workspace.name, run_id=str(hit.run_id)))
    assert state["suggest_status"] == "running"
