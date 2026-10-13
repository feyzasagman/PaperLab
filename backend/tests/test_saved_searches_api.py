import uuid

import pytest

from app.core import queue, saved_searches

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

DAILY = {"query": "bert", "sources": ["arxiv"], "schedule_kind": "daily", "time": "02:00", "timezone": "Europe/Berlin"}


@pytest.fixture
async def workspace_id(client):
    return (await client.post("/api/workspaces", json={"name": "Saved API"})).json()["id"]


@pytest.fixture
def base(workspace_id):
    return f"/api/workspaces/{workspace_id}/saved-searches"


async def test_create_and_list(client, base):
    resp = await client.post(base, json=DAILY)
    assert resp.status_code == 201
    body = resp.json()
    assert (body["schedule_kind"], body["time"], body["schedule"]) == ("daily", "02:00", "0 2 * * *")
    assert body["next_run_at"]
    listed = await client.get(base)
    assert [s["id"] for s in listed.json()] == [body["id"]]


async def test_patch_disable_and_weekly(client, base):
    created = (await client.post(base, json=DAILY)).json()
    off = await client.patch(f"{base}/{created['id']}", json={"enabled": False})
    assert off.status_code == 200 and off.json()["next_run_at"] is None
    body = {"schedule_kind": "weekly", "time": "02:00", "weekday": 3}
    weekly = await client.patch(f"{base}/{created['id']}", json=body)
    assert weekly.json()["schedule"] == "0 2 * * 3"


async def test_patch_time_only_keeps_stored_weekday(client, base):
    created = (await client.post(base, json={**DAILY, "schedule_kind": "weekly", "weekday": 3})).json()
    resp = await client.patch(f"{base}/{created['id']}", json={"time": "05:30"})
    assert resp.json()["schedule"] == "30 5 * * 3"


async def test_run_now_enqueues_a_job(client, base, arq, monkeypatch):
    monkeypatch.setattr(queue, "_pool", arq)
    created = (await client.post(base, json=DAILY)).json()
    resp = await client.post(f"{base}/{created['id']}/run")
    assert resp.status_code == 201
    assert arq.jobs == [("run_workspace_search", resp.json()["id"])]


async def test_delete_then_list_empty(client, base):
    created = (await client.post(base, json=DAILY)).json()
    assert (await client.delete(f"{base}/{created['id']}")).status_code == 204
    assert (await client.get(base)).json() == []


async def test_foreign_workspace_is_404(client):
    resp = await client.get(f"/api/workspaces/{uuid.uuid4()}/saved-searches")
    assert resp.status_code == 404


async def test_bad_cron_is_422(client, base):
    resp = await client.post(base, json={**DAILY, "schedule_kind": "custom", "cron": "bad"})
    assert resp.status_code == 422
    assert "invalid_schedule" in resp.json()["detail"]


async def test_unknown_time_zone_is_422(client, base):
    resp = await client.post(base, json={**DAILY, "timezone": "Mars/Base"})
    assert resp.status_code == 422
    assert "invalid_timezone" in resp.json()["detail"]


@pytest.mark.parametrize("field", ["query", "sources", "timezone", "enabled"])
async def test_patch_null_is_422(client, base, field):
    created = (await client.post(base, json=DAILY)).json()
    resp = await client.patch(f"{base}/{created['id']}", json={field: None})
    assert resp.status_code == 422


async def test_patch_null_year_clears_the_range(client, base):
    created = (await client.post(base, json={**DAILY, "year_from": 2020})).json()
    resp = await client.patch(f"{base}/{created['id']}", json={"year_from": None})
    assert resp.status_code == 200 and resp.json()["year_from"] is None


async def test_other_workspaces_saved_search_is_404(client, base, arq, monkeypatch):
    monkeypatch.setattr(queue, "_pool", arq)
    created = (await client.post(base, json=DAILY)).json()
    other = (await client.post("/api/workspaces", json={"name": "Other"})).json()["id"]
    url = f"/api/workspaces/{other}/saved-searches/{created['id']}"
    assert (await client.patch(url, json={"enabled": False})).status_code == 404
    assert (await client.delete(url)).status_code == 404
    assert (await client.post(f"{url}/run")).status_code == 404


async def test_run_now_with_no_source_is_422(client, base, arq, monkeypatch):
    async def none_on(_session):
        return []

    monkeypatch.setattr(queue, "_pool", arq)
    monkeypatch.setattr(saved_searches, "_sources_on", none_on)
    created = (await client.post(base, json=DAILY)).json()
    resp = await client.post(f"{base}/{created['id']}/run")
    assert resp.status_code == 422 and arq.jobs == []
