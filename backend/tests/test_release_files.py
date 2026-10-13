"""The files a release is built from (spec §3, §4, §7): the build context, the compose file the desktop app writes out,
and the release workflow. Each test pins the spec's values; the builds themselves run in the plan and in CI."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# The api image holds only backend/, so there is no checkout around this file there.
pytestmark = pytest.mark.skipif(not (ROOT / "docker-compose.yml").exists(), reason="needs the repository checkout")


def test_the_build_context_leaves_out_every_env_file_and_every_local_folder():
    """Docker reads these patterns from the context root: a bare `.env` or `node_modules` would still let
    frontend/.env* and frontend/node_modules into the image (spec §3)."""
    patterns = set((ROOT / ".dockerignore").read_text().split())
    required = {"**/.env", "**/.env.*", "**/node_modules", "**/.venv", ".git", "docs", "desktop", "website", "frontend/dist"}

    assert required <= patterns
    assert {".env", "node_modules", ".venv"} & patterns == set()


def test_the_image_builds_the_frontend_from_the_committed_api_types_and_serves_it():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "RUN npx tsc -b && npx vite build" in dockerfile
    assert "npm run build" not in dockerfile  # its prebuild regenerates the types from a running API
    assert "FRONTEND_DIST=/app/web" in dockerfile
    assert dockerfile.index("COPY --from=deps /venv /venv") < dockerfile.index("COPY backend/ ./")  # venv, then code


APP_IMAGE = "ghcr.io/khannoussi-malek/paperlab:${PAPERLAB_VERSION:-dev}"
OPTIONAL_ENV = [{"path": ".env", "required": False}]
WORKERS = ("worker", "ingest-worker")
APP_SERVICES = ("api", *WORKERS)


def compose(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())


def test_the_release_stack_runs_the_published_image_and_pinned_databases():
    release = compose("desktop/docker-compose.yml")
    services = release["services"]

    assert release["name"] == "paperlab-app"
    assert set(services) == {"db", "redis", "migrate", "api", "worker", "ingest-worker"}
    assert services["db"]["image"] == "pgvector/pgvector:0.8.6-pg16"
    assert services["redis"]["image"] == "redis:7.4.11-alpine"
    assert [services[name]["image"] for name in ("migrate", *APP_SERVICES)] == [APP_IMAGE] * 4
    assert "latest" not in (ROOT / "desktop/docker-compose.yml").read_text()
    for name, service in services.items():
        assert "build" not in service, name
        # Nothing starts with the computer: on-failure brings a crashed worker back, but never on Docker's own start.
        assert service.get("restart", "no") == ("on-failure" if name in WORKERS else "no"), name
        assert all(volume.split(":")[0] in release["volumes"] for volume in service.get("volumes", [])), name


def test_migrate_runs_once_before_the_api_and_the_worker_which_run_as_pid_1():
    services = compose("desktop/docker-compose.yml")["services"]

    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["migrate"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert services["api"]["command"] == ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    assert services["worker"]["command"] == ["arq", "app.workers.settings.WorkerSettings"]
    assert services["ingest-worker"]["command"] == ["arq", "app.workers.settings.IngestWorkerSettings"]
    for name in APP_SERVICES:
        assert services[name]["depends_on"]["migrate"] == {"condition": "service_completed_successfully"}
        assert services[name]["extra_hosts"] == ["host.docker.internal:host-gateway"]
        assert services[name]["environment"]["LLM_PROVIDER"] == "${LLM_PROVIDER:-ollama}"
        assert services[name]["environment"]["DISCOVERY_PROVIDER"] == "${DISCOVERY_PROVIDER:-live}"
    assert services["api"]["environment"]["PAPERLAB_DIR"] == "${PAPERLAB_DIR:-${PWD:-}}"


def test_only_the_api_is_published_and_only_on_this_computer():
    services = compose("desktop/docker-compose.yml")["services"]

    published = {name: service["ports"] for name, service in services.items() if "ports" in service}
    assert published == {"api": ["127.0.0.1:${PAPERLAB_PORT:-5190}:8000"]}


def test_the_library_lives_in_named_volumes_and_redis_keeps_its_queue():
    release = compose("desktop/docker-compose.yml")
    services = release["services"]

    assert set(release["volumes"]) == {"pgdata", "pdfs", "redisdata", "models"}
    assert services["redis"]["command"] == ["redis-server", "--appendonly", "yes"]
    assert services["redis"]["volumes"] == ["redisdata:/data"]
    for name in APP_SERVICES:
        assert "models:/models" in services[name]["volumes"]


def test_the_dev_stack_runs_the_ingest_worker_beside_the_worker():
    services = compose("docker-compose.yml")["services"]

    assert "app.workers.settings.WorkerSettings" in services["worker"]["command"]
    assert "app.workers.settings.IngestWorkerSettings" in services["ingest-worker"]["command"]
    assert services["ingest-worker"]["environment"] == services["worker"]["environment"]


def test_no_env_file_is_needed_in_either_stack():
    """Every setting has a default in config.py, so `cp .env.example .env` left the Quick start."""
    release = compose("desktop/docker-compose.yml")["services"]

    assert [release[name]["env_file"] for name in ("migrate", *APP_SERVICES)] == [OPTIONAL_ENV] * 4
    assert compose("docker-compose.yml")["x-backend"]["env_file"] == OPTIONAL_ENV


def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


def test_a_version_tag_releases_and_a_change_to_the_release_files_is_a_dry_run():
    triggers = workflow()[True]  # YAML 1.1 reads the key `on` as true

    assert triggers["push"] == {"tags": ["v*"]}
    assert "workflow_dispatch" in triggers
    assert triggers["pull_request"]["paths"] == [
        ".github/workflows/release.yml",
        "desktop/**",
        "Dockerfile",
        ".dockerignore",
    ]
    assert [path.name for path in (ROOT / ".github/workflows").iterdir()] == ["release.yml"]  # the only workflow


def test_images_build_natively_per_architecture_and_are_tagged_with_the_version_only():
    jobs = workflow()["jobs"]
    legs = jobs["image"]["strategy"]["matrix"]["include"]
    text = (ROOT / ".github/workflows/release.yml").read_text()

    assert [(leg["platform"], leg["runner"]) for leg in legs] == [
        ("linux/amd64", "ubuntu-latest"),
        ("linux/arm64", "ubuntu-24.04-arm"),
    ]
    assert jobs["image"]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["release"]["permissions"] == {"contents": "write"}
    assert re.search(r"paperlab:latest|:latest\b", text) is None
    assert "ghcr.io/khannoussi-malek/paperlab" in text  # lowercase: registries refuse capitals


def test_the_version_is_validated_once_and_reaches_the_manifest_job_through_env_not_interpolation():
    """Both check's tag and the version sed replaces into come from desktop/package.json, so it is validated once,
    in check; a shell command built from it (here, the manifest job's image tag) takes it through env, never
    interpolated into the run: script directly."""
    jobs = workflow()["jobs"]

    assert "*[!0-9.]*" in jobs["check"]["steps"][-1]["run"]  # anything but digits and dots fails the job

    tag_step = next(step for step in jobs["manifest"]["steps"] if "imagetools create" in step.get("run", ""))
    assert tag_step["env"] == {"VERSION": "${{ needs.check.outputs.version }}"}
    assert "$IMAGE:$VERSION" in tag_step["run"]
    assert "needs.check.outputs.version" not in tag_step["run"]


def test_each_system_builds_its_installers_and_the_release_stays_a_draft():
    jobs = workflow()["jobs"]
    text = (ROOT / ".github/workflows/release.yml").read_text()

    assert [leg["os"] for leg in jobs["installers"]["strategy"]["matrix"]["include"]] == [
        "macos-latest",
        "windows-latest",
        "ubuntu-latest",
    ]
    assert "desktop/package.json" in jobs["check"]["steps"][-1]["run"]  # the tag must be the app's version
    assert "gh release create" in text and "--draft" in text
