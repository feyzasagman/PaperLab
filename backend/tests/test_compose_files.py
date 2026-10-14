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
