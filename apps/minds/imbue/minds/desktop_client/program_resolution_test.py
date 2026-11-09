import os
import stat
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.program_resolution import LoginShellPath
from imbue.minds.desktop_client.program_resolution import expand_program_home
from imbue.minds.desktop_client.program_resolution import is_interpreter_program
from imbue.minds.desktop_client.program_resolution import resolve_program


@pytest.mark.parametrize(
    "program", ["bash", "zsh", "python3", "python3.12", "node", "uv", "npx", "/bin/sh", "~/bin/python", "osascript"]
)
def test_shells_runtimes_and_runners_are_interpreters(program: str) -> None:
    assert is_interpreter_program(program)


@pytest.mark.parametrize("program", ["rsync", "gh", "~/.studio/custom-scripts/deploy.sh", "/opt/homebrew/bin/jq"])
def test_ordinary_programs_are_not_interpreters(program: str) -> None:
    assert not is_interpreter_program(program)


def test_a_home_rooted_program_expands_and_others_stay(tmp_path: Path) -> None:
    assert expand_program_home("~/.studio/custom-scripts/deploy.sh", tmp_path) == str(
        tmp_path / ".studio/custom-scripts/deploy.sh"
    )
    assert expand_program_home("rsync", tmp_path) == "rsync"
    assert expand_program_home("/usr/bin/env", tmp_path) == "/usr/bin/env"


def test_a_bare_name_resolves_through_the_given_path_and_a_rooted_one_as_itself(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "tool-36298"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    (tmp_path / ".studio" / "custom-scripts").mkdir(parents=True)
    script = tmp_path / ".studio" / "custom-scripts" / "deploy.sh"
    script.write_text("#!/bin/sh\n")

    found = resolve_program("tool-36298", tmp_path, str(bin_dir))
    missing = resolve_program("tool-36298", tmp_path, "/nonexistent-36298")
    rooted = resolve_program("~/.studio/custom-scripts/deploy.sh", tmp_path, str(bin_dir))
    absent = resolve_program("~/.studio/custom-scripts/later.sh", tmp_path, str(bin_dir))

    assert found.path == tool
    assert missing.path is None
    assert rooted.path == script
    assert absent.path is None
    assert not found.is_interpreter


def test_the_login_shell_path_is_the_shells_last_stdout_line_and_is_reused(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """A profile may chat on stdout before the PATH is printed; a second read within the TTL starts no second shell."""
    starts = tmp_path / "starts"
    shell = tmp_path / "shell-36299"
    shell.write_text(
        f'#!/bin/sh\necho started >> "{starts}"\necho "Welcome from a profile"\nprintf %s "/login-shell-36299:/usr/bin"\n'
    )
    shell.chmod(0o700)
    environment = {**os.environ, "SHELL": str(shell)}
    reader = LoginShellPath(concurrency_group=root_concurrency_group, environment=environment, ttl_seconds=60.0)

    first = reader.read()
    second = reader.read()

    assert first == "/login-shell-36299:/usr/bin"
    assert second == first
    assert starts.read_text() == "started\n"


def test_a_real_login_shell_answers_with_its_path(root_concurrency_group: ConcurrencyGroup) -> None:
    """Whatever a host's profile does to PATH (Debian resets it, macOS prepends to it), the system directories are on it."""
    reader = LoginShellPath(concurrency_group=root_concurrency_group, environment={**os.environ, "SHELL": "/bin/sh"})

    assert {"/bin", "/usr/bin"} & set(reader.read().split(os.pathsep))


def test_a_shell_that_cannot_run_falls_back_to_the_desktops_own_path(root_concurrency_group: ConcurrencyGroup) -> None:
    environment = {**os.environ, "SHELL": "/nonexistent/shell-36300", "PATH": "/own-path-36300"}
    reader = LoginShellPath(concurrency_group=root_concurrency_group, environment=environment)

    assert reader.read() == "/own-path-36300"
