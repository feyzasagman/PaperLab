"""scripts/paperlab-mcp, the launcher MCP clients start on macOS, Linux and WSL, run with `sh` against a fake docker."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "paperlab-mcp"

# The api image holds only backend/, so there is no checkout around this file there.
pytestmark = pytest.mark.skipif(not (ROOT / "docker-compose.yml").exists(), reason="needs the repository checkout")

FAKE_DOCKER = """#!/bin/sh
printf '%s|%s\\n' "$(pwd)" "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "compose ps") printf '%s' "$FAKE_PS"; printf '%s' "$FAKE_PS_ERR" >&2; exit "${FAKE_PS_STATUS:-0}" ;;
  "compose exec") echo "the server's own output" ;;
esac
"""


def run_script(tmp_path: Path, script: Path = SCRIPT, **fake) -> tuple[subprocess.CompletedProcess, list[str]]:
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {**os.environ, "PAPERLAB_DOCKER": str(docker), "FAKE_LOG": str(log), **fake}
    result = subprocess.run(
        ["sh", str(script)], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30
    )
    return result, log.read_text().splitlines() if log.exists() else []


def test_with_api_running_it_execs_the_server_in_the_api_container_from_the_repository_root(tmp_path):
    result, calls = run_script(tmp_path, FAKE_PS="db\nredis\napi\nworker\nfrontend\n")

    assert calls == [
        f"{ROOT}|compose ps --status running --services",
        f"{ROOT}|compose exec -T api python -m mcp_server",
    ]
    assert (result.returncode, result.stdout, result.stderr) == (0, "the server's own output\n", "")


def test_with_api_not_running_it_says_how_to_start_paperlab(tmp_path):
    result, calls = run_script(tmp_path, FAKE_PS="db\nredis\nworker\n")

    assert len(calls) == 1
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f'paperlab-mcp: PaperLab isn\'t running. Open the PaperLab app, or run "docker compose up -d" in {ROOT}.\n'
    )


def test_when_docker_refuses_access_it_names_the_docker_group(tmp_path):
    refused = "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
    result, calls = run_script(tmp_path, FAKE_PS_ERR=refused, FAKE_PS_STATUS="1")

    assert len(calls) == 1
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        "paperlab-mcp: Docker refused access (permission denied). On Linux, add your user to the docker group: "
        "sudo usermod -aG docker $USER, then log out and back in.\n"
    )


def test_when_docker_is_not_found_it_says_how_to_install_it(tmp_path):
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",  # Keep sh but no docker
            "PAPERLAB_DOCKER_PATHS": "",  # Disable the fallback paths
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        "paperlab-mcp: docker was not found. Install Docker, or set PAPERLAB_DOCKER to its full path.\n"
    )


def test_the_script_is_executable_and_checked_out_with_lf_line_endings():
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    assert b"\r" not in SCRIPT.read_bytes()
    assert "scripts/paperlab-mcp text eol=lf" in (ROOT / ".gitattributes").read_text().splitlines()


def test_from_the_apps_data_folder_it_runs_compose_there(tmp_path):
    """The desktop app's data folder and the release tarball hold scripts/paperlab-mcp beside docker-compose.yml. The
    folder has a space in it, like macOS's Application Support."""
    folder = tmp_path / "PaperLab data"
    (folder / "scripts").mkdir(parents=True)
    script = folder / "scripts" / "paperlab-mcp"
    shutil.copy2(SCRIPT, script)
    (folder / "docker-compose.yml").write_text("name: paperlab-app\n")

    result, calls = run_script(tmp_path, script=script, FAKE_PS="db\nredis\n")

    assert calls == [f"{folder}|compose ps --status running --services"]
    assert result.stderr == (
        f'paperlab-mcp: PaperLab isn\'t running. Open the PaperLab app, or run "docker compose up -d" in {folder}.\n'
    )


@pytest.mark.skipif(Path("/usr/bin/docker").exists(), reason="a docker in /usr/bin is on this test's PATH")
@pytest.mark.parametrize("place", [".docker/bin/docker", ".rd/bin/docker"])
def test_docker_desktops_and_rancher_desktops_per_user_docker_are_found_without_path(tmp_path, place):
    """Docker Desktop's per-user install and Rancher Desktop put docker in the home folder, which an app started from
    the Dock never has on PATH."""
    home = tmp_path / "home"
    docker = home / place
    docker.parent.mkdir(parents=True)
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "calls.log"

    result = subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=tmp_path,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "FAKE_LOG": str(log), "FAKE_PS": "api\n"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines()[0] == f"{ROOT}|compose ps --status running --services"


def test_install_writes_a_launcher_in_local_bin_that_runs_this_folders_script(tmp_path):
    """`--install` puts ~/.local/bin/paperlab-mcp in place, so Claude's config and command are the same for everyone.
    The folder has a ' and a space in it; the wrapper still runs compose from there."""
    folder = tmp_path / "Bob's PaperLab"
    (folder / "scripts").mkdir(parents=True)
    script = folder / "scripts" / "paperlab-mcp"
    shutil.copy2(SCRIPT, script)
    home = tmp_path / "home"
    home.mkdir()

    installed = subprocess.run(
        ["sh", str(script), "--install"],
        cwd=tmp_path,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    launcher = home / ".local" / "bin" / "paperlab-mcp"
    assert (installed.returncode, installed.stdout) == (0, "")
    assert installed.stderr == f"paperlab-mcp: installed {launcher}, which runs {script}.\n"
    assert launcher.stat().st_mode & 0o777 == 0o755

    result, calls = run_script(tmp_path, script=launcher, HOME=str(home), FAKE_PS="api\n")

    assert calls == [
        f"{folder}|compose ps --status running --services",
        f"{folder}|compose exec -T api python -m mcp_server",
    ]
    assert (result.returncode, result.stdout) == (0, "the server's own output\n")
