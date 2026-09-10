"""Unit tests for the mngr_ttyd plugin."""

import importlib.resources
import re
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr import resources as mngr_resources
from imbue.mngr.interfaces.host import NamedCommand
from imbue.mngr_ttyd.plugin import TTYD_COMMAND
from imbue.mngr_ttyd.plugin import TTYD_ENSURE_INSTALLED_COMMAND
from imbue.mngr_ttyd.plugin import TTYD_INDEX_FILENAME
from imbue.mngr_ttyd.plugin import TTYD_INSTALL_DIR
from imbue.mngr_ttyd.plugin import TTYD_SERVICE_NAME
from imbue.mngr_ttyd.plugin import TTYD_VERSION
from imbue.mngr_ttyd.plugin import TTYD_WINDOW_NAME
from imbue.mngr_ttyd.plugin import build_ttyd_ensure_installed_command
from imbue.mngr_ttyd.plugin import on_after_provisioning
from imbue.mngr_ttyd.plugin import override_command_options


class _DummyCommandClass:
    pass


class _FakeTtydHost:
    """Fake host for testing on_after_provisioning.

    Tracks executed commands and written files. By default, all commands succeed.
    Set ttyd_installed=False to simulate ttyd not being installed on the host.
    """

    def __init__(self, host_dir: Path, *, ttyd_installed: bool = True, ttyd_failure_reason: str = "") -> None:
        self.host_dir = host_dir
        self._ttyd_installed = ttyd_installed
        self._ttyd_failure_reason = ttyd_failure_reason
        self.executed_cmds: list[str] = []
        self.written_files: list[tuple[Path, bytes, str]] = []

    def _execute_command(self, cmd: str, **kwargs: Any) -> SimpleNamespace:
        self.executed_cmds.append(cmd)
        if "command -v ttyd" in cmd and not self._ttyd_installed:
            return SimpleNamespace(returncode=1, success=False, stdout="", stderr=self._ttyd_failure_reason)
        return SimpleNamespace(returncode=0, success=True, stdout="", stderr="")

    def execute_idempotent_command(self, cmd: str, **kwargs: Any) -> SimpleNamespace:
        return self._execute_command(cmd, **kwargs)

    def execute_stateful_command(self, cmd: str, **kwargs: Any) -> SimpleNamespace:
        return self._execute_command(cmd, **kwargs)

    def write_file(self, path: Path, content: bytes, mode: str = "0644") -> None:
        self.written_files.append((path, content, mode))


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


def test_ttyd_command_is_parseable_as_named_command() -> None:
    """Verify that the injected command string can be parsed by NamedCommand.from_string."""
    params: dict[str, Any] = {}

    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )

    named_cmd = NamedCommand.from_string(params["extra_window"][0])
    assert named_cmd.window_name == TTYD_WINDOW_NAME
    assert str(named_cmd.command) == TTYD_COMMAND


def test_ttyd_command_uses_random_port() -> None:
    """Verify that the ttyd command binds to a random port via -p 0."""
    assert "ttyd -p 0" in TTYD_COMMAND


def test_ttyd_command_enables_url_arg_dispatch() -> None:
    """Verify that the ttyd command uses -a for URL-arg dispatch."""
    assert "ttyd -p 0 -a" in TTYD_COMMAND


def test_ttyd_command_writes_service_log() -> None:
    """Verify that the ttyd command writes to services/events.jsonl for forwarding service discovery."""
    assert "services/events.jsonl" in TTYD_COMMAND
    assert TTYD_SERVICE_NAME in TTYD_COMMAND
    assert "MNGR_AGENT_STATE_DIR" in TTYD_COMMAND
    assert "service_registered" in TTYD_COMMAND
    assert "timestamp" in TTYD_COMMAND
    assert "event_id" in TTYD_COMMAND


def test_ttyd_command_watches_stderr_for_port() -> None:
    """Verify that the command parses the port from ttyd's output."""
    assert "Listening on port:" in TTYD_COMMAND


def test_ttyd_command_skips_log_when_no_state_dir() -> None:
    """Verify that the command gracefully handles MNGR_AGENT_STATE_DIR being unset."""
    assert 'if [ -n "$MNGR_AGENT_STATE_DIR" ]' in TTYD_COMMAND


def test_ttyd_command_dispatches_to_ttyd_scripts() -> None:
    """Verify that the dispatch script routes to commands/ttyd/<KEY>.sh."""
    assert "commands/ttyd/$KEY.sh" in TTYD_COMMAND


def test_ttyd_command_scans_ttyd_scripts_for_events() -> None:
    """Verify that the port wrapper scans commands/ttyd/*.sh and writes events for each."""
    assert 'commands/ttyd/"*.sh' in TTYD_COMMAND
    assert "basename" in TTYD_COMMAND
    assert "?arg=$_K" in TTYD_COMMAND


# -- on_after_provisioning tests --


def test_on_after_provisioning_writes_agent_script(tmp_path: Path) -> None:
    """Verify that on_after_provisioning writes ttyd/agent.sh to the agent state dir."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    agent_id = "test-agent-123"

    host = _FakeTtydHost(host_dir)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id=agent_id)), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    script_writes = [w for w in host.written_files if w[0].name == "agent.sh"]
    assert len(script_writes) == 1
    script_path, content, mode = script_writes[0]
    assert script_path == host_dir / "agents" / agent_id / "commands" / "ttyd" / "agent.sh"
    assert mode == "0755"
    assert b"#!/bin/bash" in content
    assert b"tmux attach" in content


def test_on_after_provisioning_installs_web_client(tmp_path: Path) -> None:
    """Verify that on_after_provisioning installs the OSC 52-capable web client served via -I."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    agent_id = "test-agent-456"

    host = _FakeTtydHost(host_dir)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id=agent_id)), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    index_writes = [w for w in host.written_files if w[0].name == TTYD_INDEX_FILENAME]
    assert len(index_writes) == 1
    index_path, content, mode = index_writes[0]
    assert index_path == host_dir / "agents" / agent_id / "commands" / "ttyd" / TTYD_INDEX_FILENAME
    # Served as a static asset, so it must not be executable.
    assert mode == "0644"
    # The vendored client must actually register an OSC 52 handler and carry the
    # empty-target patch, otherwise tmux copies still never reach the clipboard.
    text = content.decode()
    assert "registerOscHandler(52" in text
    assert "isSystemSelection" in text


def test_ttyd_command_serves_custom_client_via_index() -> None:
    """Verify the ttyd command conditionally serves the custom client via -I."""
    # The client path is under the agent's commands/ttyd dir.
    assert f"commands/ttyd/{TTYD_INDEX_FILENAME}" in TTYD_COMMAND
    # -I is added only when the file exists, so a missing client falls back to the
    # built-in one rather than ttyd refusing to start.
    assert '$([ -f "$_TTYD_INDEX" ] && echo -I "$_TTYD_INDEX")' in TTYD_COMMAND


def test_agent_script_honors_target_agent_name_arg(tmp_path: Path) -> None:
    """Verify that the ttyd agent.sh routes to a named agent's tmux session when $1 is set.

    The frontend passes the agent name as a second URL arg (?arg=agent&arg=<name>).
    The dispatch script must attach to the session "${MNGR_PREFIX}<name>" so that
    users can deep-link to a sub-agent's terminal rather than always landing on
    the primary agent's session where ttyd itself runs.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host = _FakeTtydHost(host_dir)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    _, content, _ = host.written_files[0]
    script = content.decode()
    # Honors $1 as target agent name.
    assert '_TARGET_AGENT="${1:-}"' in script
    # Uses MNGR_PREFIX when building the session name.
    assert "MNGR_PREFIX" in script
    # Falls back to the ambient session when $1 is empty.
    assert "display-message -p '#{session_name}'" in script


def test_agent_script_targets_primary_window_by_name_not_index(tmp_path: Path) -> None:
    """The attach must target the agent's named primary window, never the literal :0 index.

    mngr names the primary window (tmux.primary_window_name, default "agent") and
    targets it by name everywhere so it works regardless of the user's tmux base-index;
    the ttyd attach must do the same. The window name is read from
    MNGR_PRIMARY_WINDOW_NAME (exported into the agent env) with an "agent" default.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host = _FakeTtydHost(host_dir)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    _, content, _ = host.written_files[0]
    script = content.decode()
    assert '_WINDOW="${MNGR_PRIMARY_WINDOW_NAME:-agent}"' in script
    assert 'attach -t "=$_SESSION:$_WINDOW"' in script
    # The attach must not target the literal :0 window index.
    assert ':0"' not in script


def test_on_after_provisioning_creates_ttyd_directory(tmp_path: Path) -> None:
    """Verify that on_after_provisioning creates the commands/ttyd/ directory."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = _FakeTtydHost(host_dir)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    assert any("mkdir -p" in cmd and "commands/ttyd" in cmd for cmd in host.executed_cmds)


# -- ttyd install tests --
#
# The install command is a shell script, so the tests that matter run it for real under `sh`
# against a stubbed PATH: the stand-ins record what was invoked, which is how each host shape
# gets simulated without touching the real /usr/local/bin or the network.

_STUB_DIR_NAME = "stubs"
_INSTALL_DIR_NAME = "bin"
_TMP_DIR_NAME = "tmp"
_MARKER_DIR_NAME = "markers"

# Records that it ran, then honors `-o <path>` so the download lands where the real curl would.
_CURL_STUB = """: > "$TTYD_TEST_MARKER_DIR/curl"
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then echo fake-ttyd-binary > "$2"; fi
  shift
done"""

# Stands in for a host where sudo demands a password, so `sudo -n` fails as it does on a
# stock macOS or a locked-down VM.
_SUDO_STUB = """: > "$TTYD_TEST_MARKER_DIR/sudo"
echo "sudo: a password is required" >&2
exit 1"""

_DARWIN_UNAME_STUB = 'if [ "$1" = "-s" ]; then echo Darwin; else echo arm64; fi'
_LINUX_UNAME_STUB = 'if [ "$1" = "-s" ]; then echo Linux; else echo x86_64; fi'


def _run_ensure_ttyd_command(
    cg: ConcurrencyGroup,
    tmp_path: Path,
    # Shell body for each binary to stub out, keyed by binary name and placed first on PATH.
    stubs: Mapping[str, str],
    # When False the install dir is never created, which is unwritable even for root (unlike
    # a chmod-ed directory, which root would still be allowed to write).
    is_install_dir_present: bool = True,
) -> FinishedProcess:
    """Run the ensure-installed command under `sh` against a stubbed PATH and install dir."""
    stub_dir = tmp_path / _STUB_DIR_NAME
    marker_dir = tmp_path / _MARKER_DIR_NAME
    tmp_dir = tmp_path / _TMP_DIR_NAME
    for directory in (stub_dir, marker_dir, tmp_dir):
        directory.mkdir(exist_ok=True)
    install_dir = tmp_path / _INSTALL_DIR_NAME
    if is_install_dir_present:
        install_dir.mkdir(exist_ok=True)

    for name, body in stubs.items():
        stub_path = stub_dir / name
        stub_path.write_text(f"#!/bin/sh\n{body}\n")
        stub_path.chmod(0o755)

    return cg.run_process_to_completion(
        ["sh", "-c", build_ttyd_ensure_installed_command(str(install_dir))],
        is_checked_after=False,
        # A minimal PATH keeps the real ttyd out of the way: the offload image preinstalls it
        # into /usr/local/bin, which is deliberately excluded here.
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "TMPDIR": str(tmp_dir),
            "TTYD_TEST_MARKER_DIR": str(marker_dir),
        },
    )


def _was_invoked(tmp_path: Path, binary: str) -> bool:
    return (tmp_path / _MARKER_DIR_NAME / binary).exists()


def test_ensure_command_does_nothing_when_ttyd_is_already_on_path(cg: ConcurrencyGroup, tmp_path: Path) -> None:
    """An already-installed ttyd must short-circuit before any download."""
    result = _run_ensure_ttyd_command(
        cg, tmp_path, stubs={"ttyd": "exit 0", "curl": _CURL_STUB, "sudo": _SUDO_STUB, "uname": _LINUX_UNAME_STUB}
    )

    assert result.returncode == 0
    assert not _was_invoked(tmp_path, "curl")


def test_ensure_command_skips_the_download_when_the_platform_has_no_prebuilt_binary(
    cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    """The ttyd release ships Linux binaries only, so macOS must not fetch one it cannot run."""
    result = _run_ensure_ttyd_command(
        cg, tmp_path, stubs={"uname": _DARWIN_UNAME_STUB, "curl": _CURL_STUB, "sudo": _SUDO_STUB}
    )

    assert result.returncode != 0
    assert not _was_invoked(tmp_path, "curl")
    assert "brew install ttyd" in result.stderr


def test_ensure_command_skips_the_download_when_the_install_dir_cannot_be_written(
    cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    """With nowhere to put the binary and no passwordless sudo, the download must not happen."""
    result = _run_ensure_ttyd_command(
        cg,
        tmp_path,
        stubs={"uname": _LINUX_UNAME_STUB, "curl": _CURL_STUB, "sudo": _SUDO_STUB},
        is_install_dir_present=False,
    )

    assert result.returncode != 0
    assert not _was_invoked(tmp_path, "curl")
    assert "ttyd" in result.stderr


def test_ensure_command_installs_without_sudo_when_the_install_dir_is_writable(
    cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    """A writable install dir needs no sudo, which is the case where reaching for it fails."""
    result = _run_ensure_ttyd_command(
        cg, tmp_path, stubs={"uname": _LINUX_UNAME_STUB, "curl": _CURL_STUB, "sudo": _SUDO_STUB}
    )

    assert result.returncode == 0
    assert not _was_invoked(tmp_path, "sudo")
    installed = tmp_path / _INSTALL_DIR_NAME / "ttyd"
    assert installed.read_text().strip() == "fake-ttyd-binary"
    assert installed.stat().st_mode & 0o111


def test_ensure_command_leaves_no_download_behind_when_the_install_fails(cg: ConcurrencyGroup, tmp_path: Path) -> None:
    """A failed install must not leave its download behind."""
    result = _run_ensure_ttyd_command(
        cg,
        tmp_path,
        stubs={
            "uname": _LINUX_UNAME_STUB,
            "curl": _CURL_STUB,
            "sudo": _SUDO_STUB,
            "mv": 'echo "mv: permission denied" >&2; exit 1',
        },
    )

    assert result.returncode != 0
    assert _was_invoked(tmp_path, "curl")
    assert list((tmp_path / _TMP_DIR_NAME).iterdir()) == []


def test_ensure_command_downloads_the_pinned_version_from_github() -> None:
    """Verify that the command downloads the pinned ttyd version from GitHub releases."""
    assert f"github.com/tsl0922/ttyd/releases/download/{TTYD_VERSION}/" in TTYD_ENSURE_INSTALLED_COMMAND
    assert f"_DEST_DIR={TTYD_INSTALL_DIR};" in TTYD_ENSURE_INSTALLED_COMMAND
    assert "uname -m" in TTYD_ENSURE_INSTALLED_COMMAND
    assert "chmod 0755" in TTYD_ENSURE_INSTALLED_COMMAND


def test_dockerfile_pins_the_same_ttyd_version_as_the_plugin() -> None:
    """mngr's image preinstalls ttyd, and its pin must not drift from the one installed here.

    The Dockerfile cannot read TTYD_VERSION. mngr_modal ships each instruction to Modal as its
    own dockerfile_commands call, so a build ARG expands to empty in the RUN that would use it
    -- which is why the restic and offload pins next to it are inline literals too. Asserting
    the two agree is the sync mechanism, as it is for CLAUDE_CODE_VERSION in
    apps/minds/imbue/minds/test_claude_version_alignment.py.
    """
    dockerfile = importlib.resources.files(mngr_resources).joinpath("Dockerfile").read_text()

    pinned = re.search(r"tsl0922/ttyd/releases/download/([^/]+)/ttyd\.", dockerfile)
    assert pinned is not None, "the Dockerfile no longer installs ttyd from a pinned release URL"
    assert pinned.group(1) == TTYD_VERSION, (
        f"the Dockerfile pins ttyd {pinned.group(1)} but the plugin installs {TTYD_VERSION}; bump both together"
    )


def test_on_after_provisioning_ensures_ttyd_in_a_single_host_call(tmp_path: Path) -> None:
    """A single host round trip both decides on and performs the install."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = _FakeTtydHost(host_dir, ttyd_installed=False)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    assert [cmd for cmd in host.executed_cmds if cmd == TTYD_ENSURE_INSTALLED_COMMAND] == [
        TTYD_ENSURE_INSTALLED_COMMAND
    ]


def test_on_after_provisioning_warns_with_the_hosts_reason_when_ttyd_is_unavailable(
    tmp_path: Path, log_warnings: list[str]
) -> None:
    """The host's explanation must reach the user rather than being swallowed."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = _FakeTtydHost(host_dir, ttyd_installed=False, ttyd_failure_reason="no prebuilt ttyd binary for Darwin")

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    assert any("no prebuilt ttyd binary for Darwin" in message for message in log_warnings)


def test_on_after_provisioning_is_quiet_when_ttyd_is_available(tmp_path: Path, log_warnings: list[str]) -> None:
    """Verify that a host that already has ttyd produces no warning."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    host = _FakeTtydHost(host_dir, ttyd_installed=True)

    on_after_provisioning(
        agent=cast(Any, SimpleNamespace(id="a1")), host=cast(Any, host), mngr_ctx=cast(Any, SimpleNamespace())
    )

    assert log_warnings == []
