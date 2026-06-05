"""Unit tests for the mngr_ttyd plugin."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import uuid4

import pytest

from imbue.mngr.interfaces.host import NamedCommand
from imbue.mngr_ttyd.plugin import TTYD_COMMAND
from imbue.mngr_ttyd.plugin import TTYD_INSTALL_COMMAND
from imbue.mngr_ttyd.plugin import TTYD_VERSION
from imbue.mngr_ttyd.plugin import TTYD_WINDOW_NAME
from imbue.mngr_ttyd.plugin import on_after_provisioning
from imbue.mngr_ttyd.plugin import override_command_options
from imbue.mngr_ttyd.testing import FakeTtydHost


class _DummyCommandClass:
    pass


# -- override_command_options tests --


def test_adds_ttyd_command_to_create() -> None:
    """Verify that the plugin adds a ttyd command when creating agents."""
    params: dict[str, Any] = {"extra_window": ()}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert len(params["extra_window"]) == 1
    assert TTYD_WINDOW_NAME in params["extra_window"][0]
    assert TTYD_COMMAND in params["extra_window"][0]


def test_preserves_existing_extra_windows() -> None:
    """Verify that the plugin preserves any existing extra windows."""
    params: dict[str, Any] = {"extra_window": ('monitor="htop"',)}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert len(params["extra_window"]) == 2
    assert params["extra_window"][0] == 'monitor="htop"'
    assert TTYD_COMMAND in params["extra_window"][1]


def test_does_not_modify_non_create_commands() -> None:
    """Verify that the plugin does not modify params for non-create commands."""
    params: dict[str, Any] = {"extra_window": ()}

    override_command_options(
        command_name="connect",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert params["extra_window"] == ()


def test_handles_missing_extra_window_param() -> None:
    """Verify that the plugin handles the case where extra_window is not yet in params."""
    params: dict[str, Any] = {}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    assert len(params["extra_window"]) == 1
    assert TTYD_COMMAND in params["extra_window"][0]


# -- TTYD_COMMAND / TTYD_INSTALL_COMMAND tests --


def test_ttyd_command_is_parseable_as_named_command() -> None:
    """The injected command string must round-trip through NamedCommand.from_string.

    This is the real contract: ``override_command_options`` feeds the string into the
    create flow, which parses it via ``NamedCommand.from_string``. If the window name or
    quoting were malformed, the whole feature would break here.
    """
    params: dict[str, Any] = {}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    named_cmd = NamedCommand.from_string(params["extra_window"][0])
    assert named_cmd.window_name == TTYD_WINDOW_NAME
    assert str(named_cmd.command) == TTYD_COMMAND


def test_ttyd_command_is_valid_bash_syntax() -> None:
    """The embedded ttyd shell program must be syntactically valid bash.

    ``bash -n`` parses without executing, catching syntax errors (unbalanced quotes,
    broken pipelines, etc.) that the substring assertions this replaced could never see.
    """
    result = subprocess.run(["bash", "-n", "-c", TTYD_COMMAND], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ttyd_install_command_is_valid_bash_and_pins_version() -> None:
    """The install command must be valid bash and download the pinned ttyd version.

    ``bash -n`` guards against syntax errors; the version/URL assertion checks that the
    separate ``TTYD_VERSION`` constant is actually wired into the GitHub release URL (a
    real cross-constant contract, not a restatement of a single literal).
    """
    result = subprocess.run(["bash", "-n", "-c", TTYD_INSTALL_COMMAND], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"github.com/tsl0922/ttyd/releases/download/{TTYD_VERSION}/" in TTYD_INSTALL_COMMAND


# -- on_after_provisioning tests --


def _fake_agent(agent_id: str) -> Any:
    return cast(Any, SimpleNamespace(id=agent_id))


def test_on_after_provisioning_writes_executable_agent_script(tmp_path: Path) -> None:
    """on_after_provisioning writes the packaged ttyd/agent.sh to disk, executable."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    agent_id = f"agent-{uuid4().hex}"

    host = FakeTtydHost(host_dir=host_dir)

    on_after_provisioning(agent=_fake_agent(agent_id), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace()))

    script_path = host_dir / "agents" / agent_id / "commands" / "ttyd" / "agent.sh"
    assert host.written_file_paths == [script_path]
    assert script_path.is_file()
    content = script_path.read_text()
    assert content.startswith("#!/bin/bash")
    assert "tmux attach" in content
    # The script must be executable so ttyd can run it directly.
    assert script_path.stat().st_mode & 0o111


def test_on_after_provisioning_creates_ttyd_directory(tmp_path: Path) -> None:
    """on_after_provisioning actually creates the commands/ttyd/ directory on the host."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    agent_id = f"agent-{uuid4().hex}"

    host = FakeTtydHost(host_dir=host_dir)

    on_after_provisioning(agent=_fake_agent(agent_id), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace()))

    ttyd_dir = host_dir / "agents" / agent_id / "commands" / "ttyd"
    assert ttyd_dir.is_dir()


def test_on_after_provisioning_installs_ttyd_when_missing(tmp_path: Path) -> None:
    """When ttyd is absent, provisioning runs the GitHub download install command."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = FakeTtydHost(host_dir=host_dir, is_ttyd_installed=False)

    on_after_provisioning(
        agent=_fake_agent(f"agent-{uuid4().hex}"), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    assert TTYD_INSTALL_COMMAND in host.executed_commands


def test_on_after_provisioning_skips_install_when_ttyd_present(tmp_path: Path) -> None:
    """When ttyd is already present, provisioning does not run the install command."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = FakeTtydHost(host_dir=host_dir, is_ttyd_installed=True)

    on_after_provisioning(
        agent=_fake_agent(f"agent-{uuid4().hex}"), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    assert TTYD_INSTALL_COMMAND not in host.executed_commands


# -- ttyd_agent.sh routing behavior tests --
#
# These run the real packaged dispatch script against a fake ``tmux`` placed on PATH,
# so they verify the script's actual routing behavior (which tmux session it attaches
# to) rather than grepping the script source for literals.


_FAKE_TMUX = """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
if [ "$1" = "display-message" ]; then
    printf '%s\\n' "$FAKE_AMBIENT_SESSION"
fi
exit 0
"""


def _provision_agent_script(host_dir: Path, agent_id: str) -> Path:
    host = FakeTtydHost(host_dir=host_dir)
    on_after_provisioning(agent=_fake_agent(agent_id), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace()))
    return host_dir / "agents" / agent_id / "commands" / "ttyd" / "agent.sh"


def _install_fake_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(_FAKE_TMUX)
    fake_tmux.chmod(0o755)
    log_path = tmp_path / "tmux.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log_path))
    return log_path


def test_agent_script_attaches_to_named_session_when_target_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a target agent name, the script attaches to "${MNGR_PREFIX}<name>" exactly.

    The frontend deep-links to a sub-agent via ?arg=agent&arg=<name>; the script must
    build the session name from MNGR_PREFIX and attach with tmux's ``=`` exact-match
    prefix so it never lands on a prefix-collision sibling.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    script_path = _provision_agent_script(host_dir, f"agent-{uuid4().hex}")
    log_path = _install_fake_tmux(tmp_path, monkeypatch)
    prefix = f"pfx-{uuid4().hex}-"
    target = f"sub-{uuid4().hex}"
    monkeypatch.setenv("MNGR_PREFIX", prefix)
    monkeypatch.setenv("FAKE_AMBIENT_SESSION", "should-not-be-used")

    result = subprocess.run(["bash", str(script_path), target], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    tmux_calls = log_path.read_text()
    # Attaches to the named session, not the ambient one, using exact-match "=".
    assert f"attach -t ={prefix}{target}:0" in tmux_calls
    assert "display-message" not in tmux_calls


def test_agent_script_attaches_to_ambient_session_when_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no target argument, the script attaches to the ambient tmux session.

    It must query the current session via ``display-message`` and attach to it, so a
    bare ?arg=agent link lands on the primary agent's own terminal.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    script_path = _provision_agent_script(host_dir, f"agent-{uuid4().hex}")
    log_path = _install_fake_tmux(tmp_path, monkeypatch)
    ambient = f"ambient-{uuid4().hex}"
    monkeypatch.setenv("FAKE_AMBIENT_SESSION", ambient)
    monkeypatch.delenv("MNGR_PREFIX", raising=False)

    result = subprocess.run(["bash", str(script_path)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    tmux_calls = log_path.read_text()
    # Queried the ambient session, then attached to exactly that session name.
    assert "display-message" in tmux_calls
    assert f"attach -t ={ambient}:0" in tmux_calls


def test_agent_script_is_valid_bash_syntax(tmp_path: Path) -> None:
    """The packaged ttyd/agent.sh must be syntactically valid bash."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    script_path = _provision_agent_script(host_dir, f"agent-{uuid4().hex}")

    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
