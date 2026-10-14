"""The compose files keep the owner's library apart from test stacks (D273, D274). Static: no docker needed."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def services(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())["services"]


def test_every_backend_process_learns_its_stack_name():
    for path, names in [("docker-compose.yml", ["api", "worker"]), ("desktop/docker-compose.yml", ["api", "worker"])]:
        for name in names:
            assert services(path)[name]["environment"]["PAPERLAB_STACK"] == "${COMPOSE_PROJECT_NAME}", (path, name)


def test_dev_stack_ports_are_configurable_with_the_live_defaults():
    found = {name: services("docker-compose.yml")[name]["ports"] for name in ("api", "frontend", "db")}
    assert found == {
        "api": ["127.0.0.1:${API_PORT:-8000}:8000"],
        "frontend": ["127.0.0.1:${FRONTEND_PORT:-5180}:5173"],
        "db": ["127.0.0.1:${DB_PORT:-5433}:5432"],
    }


import re

SCRIPT = ROOT / "scripts" / "e2e-stack.sh"


def test_the_e2e_script_only_ever_drives_the_test_project():
    text = SCRIPT.read_text()
    calls = re.findall(r"docker compose[^\n]*", text)
    assert calls and all("-p \"$PROJECT\"" in call for call in calls), calls
    assert 'PROJECT=paperlab-e2e' in text
    assert re.search(r"API_PORT=8100\b", text) and re.search(r"FRONTEND_PORT=5280\b", text) and re.search(r"DB_PORT=5533\b", text)


def test_the_docs_no_longer_start_fake_providers_on_the_default_project():
    for path in ("README.md", "CONTRIBUTING.md", "docker-compose.yml"):
        assert not re.search(r"LLM_PROVIDER=fake[^\n`]*docker compose (?!-p)", (ROOT / path).read_text()), path
