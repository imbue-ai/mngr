"""Unit coverage for the bug report's workspace diagnostics collection.

Exercises the host half of ``workspace_diagnostics``: the ``mngr exec`` argvs,
the staging of the collector's one base64 line into the report's private
directory, the one-line notes every failure mode resolves to, and the latchkey
gateway-tail mirror.

The exec boundary is driven through a fake ``mngr`` executable placed first on
``PATH`` -- the same stub technique ``agent_creator_test`` uses -- because
collection resolves the real binary the way every other desktop-client exec
path does. The stub prints the ``--format json`` envelopes the real mngr
prints, answering the collector exec and the ``--outer`` mirror exec
separately so a test can shape each one on its own. What the archive CONTAINS
is the collector's business, covered by the template repo's own tests; here a
tiny fixture zip stands in for it.
"""

import base64
import io
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Final

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.desktop_client.workspace_diagnostics import LATCHKEY_LOG_MAX_AGE_MINUTES
from imbue.minds.desktop_client.workspace_diagnostics import REMOTE_GATEWAY_TAIL_MIRROR_FILENAME
from imbue.minds.desktop_client.workspace_diagnostics import STAGED_ZIP_FILENAME
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_COLLECTOR_PATH
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS
from imbue.minds.desktop_client.workspace_diagnostics import _SCAN_TIMEOUT_FRACTION_OF_BUDGET
from imbue.minds.desktop_client.workspace_diagnostics import build_diagnostics_argv
from imbue.minds.desktop_client.workspace_diagnostics import build_latchkey_logs_argv
from imbue.minds.desktop_client.workspace_diagnostics import collect_workspace_diagnostics
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState
from imbue.mngr_latchkey.remote_gateway import REMOTE_GATEWAY_LOG_FILENAME
from imbue.mngr_latchkey.remote_gateway import REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote_gateway import REMOTE_TUNNEL_LOG_FILENAME

_WORKSPACE_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "3")

# The command must stay far below Linux's 128KB MAX_ARG_STRLEN cap on a SINGLE
# argv string. An earlier design base64-inlined a collector script, a secret
# scanner, its config, and the console into this one string, and a chatty
# console pushed it over the cliff: the container's bash answered "Argument
# list too long" and the ENTIRE collection silently failed. The resident
# collector exists so nothing bulky ever rides in the argv again; this bound is
# what keeps anyone from reintroducing an inline payload.
_MAX_COMMAND_BYTES: Final[int] = 4 * 1024

_LATCHKEY_TAIL_TEXT: Final[str] = (
    f"==> /root/{REMOTE_LATCHKEY_DIR_NAME}/{REMOTE_GATEWAY_LOG_FILENAME} <==\nPOST /v1/messages 200\n"
)


def _make_zip_bytes() -> bytes:
    """A tiny real archive standing in for whatever the collector packed."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("logs/system_interface.log", "interface started\n")
    return buffer.getvalue()


_ZIP_BYTES: Final[bytes] = _make_zip_bytes()
_ENCODED_ZIP: Final[str] = base64.b64encode(_ZIP_BYTES).decode("ascii")

# The mirror exec's default canned answer: the outer host was skipped (no
# reachable outer, ``--missing-outer ignore``), which is an empty outer_results
# envelope -- nothing observed.
_SKIPPED_OUTER_ENVELOPE: Final[str] = '{"outer_results": [], "skipped_agents": []}'


def _write_fake_mngr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    envelope: str = "",
    exit_code: int = 0,
    outer_envelope: str = _SKIPPED_OUTER_ENVELOPE,
    outer_exit_code: int = 0,
) -> str:
    """Install an executable ``mngr`` stub first on ``PATH`` and return its path.

    Collection resolves the binary via ``PATH`` like every other desktop-client
    exec path, so the stub is installed as a real ``mngr`` in its own bin dir.
    It prints the ``--format json`` envelopes the real mngr prints -- the
    collector exec's and, when ``--outer`` is in the argv, the mirror exec's --
    and appends each invocation's arguments to a per-exec sibling log
    (``<script>.log`` / ``<script>.outer.log``), so a test can assert both what
    the real invocation carried and that no invocation happened at all.
    """
    bin_dir = tmp_path / "fake-mngr-bin"
    bin_dir.mkdir(exist_ok=True)
    envelope_path = bin_dir / "envelope.json"
    envelope_path.write_text(envelope, encoding="utf-8")
    outer_envelope_path = bin_dir / "outer_envelope.json"
    outer_envelope_path.write_text(outer_envelope, encoding="utf-8")
    script = bin_dir / MNGR_BINARY
    script.write_text(
        "#!/bin/sh\n"
        "is_outer=0\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "--outer" ]; then is_outer=1; fi\n'
        "done\n"
        'if [ "$is_outer" = 1 ]; then log="$0.outer.log"; else log="$0.log"; fi\n'
        'for arg in "$@"; do\n'
        '  printf "%s\\n" "$arg" >> "$log"\n'
        "done\n"
        f'if [ "$is_outer" = 1 ]; then cat "{outer_envelope_path}"; exit {outer_exit_code}; fi\n'
        f'cat "{envelope_path}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return str(script)


def _read_invocations(mngr_binary: str, suffix: str = ".log") -> list[str]:
    log_path = Path(mngr_binary + suffix)
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf-8").splitlines()


def _staging_dir(tmp_path: Path) -> Path:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(exist_ok=True)
    return staging_dir


def _collect(
    tmp_path: Path,
    concurrency_group: ConcurrencyGroup,
    host_state: HostState | None = None,
    timeout_seconds: float = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS,
    latchkey_plugin_data_dir: Path | None = None,
):
    """Run a collection against whatever ``mngr`` is first on PATH, both boxes checked."""
    return collect_workspace_diagnostics(
        _WORKSPACE_AGENT_ID,
        include_logs=True,
        include_transcript=True,
        staging_dir=_staging_dir(tmp_path),
        host_state=host_state,
        concurrency_group=concurrency_group,
        timeout_seconds=timeout_seconds,
        latchkey_plugin_data_dir=latchkey_plugin_data_dir,
    )


# --- the mngr exec argvs ---------------------------------------------------


def test_build_diagnostics_argv_runs_the_collector_through_the_json_envelope() -> None:
    """``--format json`` is what separates the collector's one base64 line from
    anything mngr itself prints; ``--no-start`` keeps a bug report from booting
    a stopped workspace; ``--timeout`` caps the in-container work at the same
    budget the outer subprocess is given."""
    argv = build_diagnostics_argv(_WORKSPACE_AGENT_ID, True, True)
    assert argv[:3] == [MNGR_BINARY, "exec", str(_WORKSPACE_AGENT_ID)]
    assert argv[argv.index("--format") + 1] == "json"
    assert "--no-start" in argv
    assert "--quiet" in argv
    assert argv[argv.index("--timeout") + 1] == str(int(WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS))
    command = argv[3]
    assert command.startswith(f"python3 {WORKSPACE_COLLECTOR_PATH}")


@pytest.mark.parametrize(
    ("include_logs", "include_transcript", "expected_flags"),
    [
        (True, True, ("--logs", "--transcript")),
        (True, False, ("--logs",)),
        (False, True, ("--transcript",)),
    ],
)
def test_the_collector_is_asked_for_only_the_checked_attachments(
    include_logs: bool, include_transcript: bool, expected_flags: tuple[str, ...]
) -> None:
    """An unchecked box means the collector is never asked for that content at all."""
    command = build_diagnostics_argv(_WORKSPACE_AGENT_ID, include_logs, include_transcript)[3]
    assert tuple(flag for flag in ("--logs", "--transcript") if flag in command) == expected_flags


def test_the_scan_timeout_is_the_same_fraction_of_any_budget() -> None:
    """The in-container scan gets a fixed share of whatever budget collection runs under, stretching
    with a longer one so the scan does not starve at its production share while the exec idles.

    Derived from the constants rather than written as literals: the production budget is a tuning
    knob that has already moved twice, and pinning its arithmetic here would make a future change to
    it look like a broken test rather than the deliberate retune it is."""
    production_share = int(WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS * _SCAN_TIMEOUT_FRACTION_OF_BUDGET)
    assert f"--scan-timeout={production_share}" in build_diagnostics_argv(_WORKSPACE_AGENT_ID, True, True)[3]

    doubled = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS * 2
    sandbox_command = build_diagnostics_argv(_WORKSPACE_AGENT_ID, True, True, timeout_seconds=doubled)[3]
    assert f"--scan-timeout={int(doubled * _SCAN_TIMEOUT_FRACTION_OF_BUDGET)}" in sandbox_command
    assert production_share > 0, "the scan must get a usable share of the budget"


def test_the_commands_are_tiny_because_nothing_travels_in_them() -> None:
    """The commands must stay a few hundred bytes -- no inline payloads, ever."""
    for include_logs, include_transcript in ((True, True), (True, False), (False, True)):
        command = build_diagnostics_argv(_WORKSPACE_AGENT_ID, include_logs, include_transcript)[3]
        assert len(command.encode("utf-8")) < _MAX_COMMAND_BYTES
    assert len(build_latchkey_logs_argv(_WORKSPACE_AGENT_ID)[3].encode("utf-8")) < _MAX_COMMAND_BYTES


def test_build_latchkey_logs_argv_targets_the_outer_host() -> None:
    """The gateway tail runs on the machine hosting the container, not in it.

    ``--missing-outer ignore`` lets a workspace with no reachable outer host
    skip cleanly (nothing observed, mirror untouched), and the ``find -mmin``
    bound keeps the tail to gateway logs written to in the last day.
    """
    argv = build_latchkey_logs_argv(_WORKSPACE_AGENT_ID)
    assert argv[:3] == [MNGR_BINARY, "exec", str(_WORKSPACE_AGENT_ID)]
    assert "--outer" in argv
    assert argv[argv.index("--missing-outer") + 1] == "ignore"
    assert argv[argv.index("--format") + 1] == "json"
    assert "--no-start" in argv
    command = argv[3]
    assert f'"$HOME"/{REMOTE_LATCHKEY_DIR_NAME}' in command
    assert f"-name {REMOTE_GATEWAY_LOG_FILENAME}" in command
    assert f"-name {REMOTE_TUNNEL_LOG_FILENAME}" in command
    assert f"-mmin -{LATCHKEY_LOG_MAX_AGE_MINUTES}" in command


# --- staging the archive ---------------------------------------------------


def test_collection_stages_the_returned_zip_in_the_reports_private_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The collector's one base64 line decodes and stages verbatim, and the
    result carries no note: whatever was withheld is explained inside the
    archive itself, which is the collector's business, not this module's."""
    _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope(_ENCODED_ZIP + "\n"))

    result = _collect(tmp_path, root_concurrency_group)

    assert result.note is None
    assert result.staged_zip_path == _staging_dir(tmp_path) / STAGED_ZIP_FILENAME
    assert result.staged_zip_path.read_bytes() == _ZIP_BYTES


def test_two_collections_stage_side_by_side_instead_of_overwriting_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Concurrent reports must not clobber one another's staged files.

    Reports submit immediately and finish collecting and uploading in the
    background, so two collections can be in flight at once. Each stages into
    its own private directory, so both archives survive with their own content
    and either upload can still read the file it was handed -- and nothing ever
    has to delete anything up front.
    """
    first_staging = tmp_path / "first-staging"
    second_staging = tmp_path / "second-staging"
    _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope(_ENCODED_ZIP + "\n"))
    first = collect_workspace_diagnostics(
        _WORKSPACE_AGENT_ID,
        include_logs=True,
        include_transcript=True,
        staging_dir=first_staging,
        host_state=None,
        concurrency_group=root_concurrency_group,
    )

    second_bytes = _ZIP_BYTES + b"PK-second-trailer"
    second_encoded = base64.b64encode(second_bytes).decode("ascii")
    second_bin = tmp_path / "second-stub"
    second_bin.mkdir()
    _write_fake_mngr(second_bin, monkeypatch, envelope=exec_json_envelope(second_encoded + "\n"))
    second = collect_workspace_diagnostics(
        _WORKSPACE_AGENT_ID,
        include_logs=True,
        include_transcript=True,
        staging_dir=second_staging,
        host_state=None,
        concurrency_group=root_concurrency_group,
    )

    assert first.staged_zip_path is not None and second.staged_zip_path is not None
    assert first.staged_zip_path != second.staged_zip_path
    assert first.staged_zip_path.read_bytes() == _ZIP_BYTES
    assert second.staged_zip_path.read_bytes() == second_bytes


# --- the one-line notes ----------------------------------------------------


@pytest.mark.parametrize(
    "host_state",
    [
        HostState.STOPPING,
        HostState.STOPPED,
        HostState.PAUSED,
        HostState.CRASHED,
        HostState.FAILED,
        HostState.DESTROYED,
    ],
)
def test_a_not_running_host_short_circuits_without_spawning_a_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup, host_state: HostState
) -> None:
    """A report filed from a stopped workspace resolves immediately.

    An ``mngr exec --no-start`` against a host that is not running could only
    wait out the collection budget, so the passive host state short-circuits it:
    the stub must never be invoked -- not for the collector, and not for the
    gateway-tail mirror, whose exec targets the same not-running host.
    """
    mngr_binary = _write_fake_mngr(tmp_path, monkeypatch)

    result = _collect(tmp_path, root_concurrency_group, host_state=host_state)

    assert _read_invocations(mngr_binary) == []
    assert _read_invocations(mngr_binary, ".outer.log") == []
    assert result.staged_zip_path is None
    assert result.note is not None and host_state.value.lower() in result.note


@pytest.mark.parametrize("host_state", [None, HostState.RUNNING, HostState.STARTING, HostState.UNKNOWN])
def test_a_host_that_is_not_known_to_be_down_still_attempts_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_concurrency_group: ConcurrencyGroup,
    host_state: HostState | None,
) -> None:
    """The short-circuit is evidence-based: an absent or non-terminal state is not
    evidence the workspace is down, so collection is attempted anyway."""
    mngr_binary = _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope(_ENCODED_ZIP + "\n"))

    result = _collect(tmp_path, root_concurrency_group, host_state=host_state)

    assert _read_invocations(mngr_binary)[:1] == ["exec"]
    assert result.staged_zip_path is not None


def test_a_failed_remote_command_notes_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The envelope reporting the remote command as failed -- which is what a
    workspace whose template has no collector looks like -- resolves to a note
    quoting the remote command's own stderr: in json mode mngr exits silently,
    so the envelope is the only place the diagnosis rides."""
    _write_fake_mngr(
        tmp_path,
        monkeypatch,
        envelope=exec_json_envelope("", success=False, stderr="python3: can't open file"),
        exit_code=1,
    )

    result = _collect(tmp_path, root_concurrency_group)

    assert result.staged_zip_path is None
    assert result.note is not None and result.note.startswith("workspace collection failed")
    assert "python3: can't open file" in result.note


def test_an_exec_that_never_reached_the_agent_notes_mngrs_error_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """An exec mngr could not land -- e.g. ``--no-start`` against a host the
    passive snapshot did not know was down -- leaves the envelope's results
    empty and puts the why in ``failed_agents``; the note quotes that instead
    of pretending there was no error output."""
    failed_envelope = json.dumps(
        {"results": [], "failed_agents": [{"agent": str(_WORKSPACE_AGENT_ID), "error": "agent is not running"}]}
    )
    _write_fake_mngr(tmp_path, monkeypatch, envelope=failed_envelope, exit_code=1)

    result = _collect(tmp_path, root_concurrency_group)

    assert result.staged_zip_path is None
    assert result.note is not None and result.note.startswith("workspace collection failed")
    assert "agent is not running" in result.note


def test_an_mngr_binary_that_cannot_launch_notes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A missing binary raises at fork/exec; the report still submits, with the note.

    ``PATH`` is narrowed to a directory with no ``mngr`` in it. tmux rides
    along in the narrowed dir because the isolated-tmux-server fixture's
    teardown still needs to resolve it.
    """
    lonely_bin = tmp_path / "lonely-bin"
    lonely_bin.mkdir()
    real_tmux = shutil.which("tmux")
    if real_tmux is not None:
        (lonely_bin / "tmux").symlink_to(real_tmux)
    monkeypatch.setenv("PATH", str(lonely_bin))

    result = _collect(tmp_path, root_concurrency_group)

    assert result.staged_zip_path is None
    assert result.note is not None and result.note.startswith("workspace collection could not run")


def test_an_exec_killed_by_the_collection_budget_notes_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Driven through the real subprocess path: the stub outlives a deliberately
    tiny budget (the ``timeout_seconds`` parameter exists exactly so tests can
    reshape the production budget)."""
    bin_dir = tmp_path / "sleepy-bin"
    bin_dir.mkdir()
    script = bin_dir / MNGR_BINARY
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    result = _collect(tmp_path, root_concurrency_group, timeout_seconds=0.2)

    assert result.staged_zip_path is None
    assert result.note == "workspace collection timed out after 0s"


def test_an_unreadable_payload_notes_the_older_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A remote stdout that is not base64 -- including the JSON line an older
    template's collector prints -- costs the archive, never the report."""
    old_payload = '{"contract_version": 1, "omissions": {}}\n'
    _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope(old_payload))

    result = _collect(tmp_path, root_concurrency_group)

    assert result.staged_zip_path is None
    assert result.note is not None and "unreadable payload" in result.note


def test_an_empty_remote_stdout_notes_that_nothing_was_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope("\n"))

    result = _collect(tmp_path, root_concurrency_group)

    assert result.staged_zip_path is None
    assert result.note == "the workspace collected nothing"


# --- the latchkey gateway-tail mirror --------------------------------------


def test_the_gateway_tail_is_mirrored_into_the_latchkey_dir_for_the_group_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The tail lands as a plain ``*.log`` in the latchkey plugin data dir.

    That dir is what the existing ``latchkey_raw_logs`` Sentry attachment group
    sweeps onto every event, so the write IS the whole attachment mechanism: no
    staged file, no reservation, no bookkeeping on the report itself.
    """
    plugin_dir = tmp_path / "latchkey-plugin"
    _write_fake_mngr(
        tmp_path,
        monkeypatch,
        envelope=exec_json_envelope(_ENCODED_ZIP + "\n"),
        outer_envelope=exec_json_envelope(_LATCHKEY_TAIL_TEXT, results_key="outer_results"),
    )

    result = _collect(tmp_path, root_concurrency_group, latchkey_plugin_data_dir=plugin_dir)

    mirror = plugin_dir / REMOTE_GATEWAY_TAIL_MIRROR_FILENAME
    assert mirror.read_text(encoding="utf-8") == _LATCHKEY_TAIL_TEXT
    # The report's own result knows nothing of the mirror.
    assert result.staged_zip_path is not None and result.note is None


def test_a_fresh_empty_tail_removes_a_stale_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A gateway observed to have nothing must not keep impersonating last week's tail."""
    plugin_dir = tmp_path / "latchkey-plugin"
    plugin_dir.mkdir()
    stale_mirror = plugin_dir / REMOTE_GATEWAY_TAIL_MIRROR_FILENAME
    stale_mirror.write_text("yesterday's requests\n", encoding="utf-8")
    _write_fake_mngr(
        tmp_path,
        monkeypatch,
        envelope=exec_json_envelope(_ENCODED_ZIP + "\n"),
        outer_envelope=exec_json_envelope("", results_key="outer_results"),
    )

    _collect(tmp_path, root_concurrency_group, latchkey_plugin_data_dir=plugin_dir)

    assert not stale_mirror.exists()


def test_an_unobserved_gateway_leaves_the_existing_mirror_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """An empty outer_results envelope (``--missing-outer ignore`` skipped the
    workspace: no reachable outer host) is not evidence the gateway has
    nothing, so the last mirror stands."""
    plugin_dir = tmp_path / "latchkey-plugin"
    plugin_dir.mkdir()
    existing_mirror = plugin_dir / REMOTE_GATEWAY_TAIL_MIRROR_FILENAME
    existing_mirror.write_text("the last observed tail\n", encoding="utf-8")
    _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope(_ENCODED_ZIP + "\n"))

    result = _collect(tmp_path, root_concurrency_group, latchkey_plugin_data_dir=plugin_dir)

    assert existing_mirror.read_text(encoding="utf-8") == "the last observed tail\n"
    # The report itself is untouched by the skipped mirror refresh.
    assert result.staged_zip_path is not None


def test_no_mirror_exec_spawns_when_latchkey_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A minimal app without latchkey (no plugin data dir) never runs the outer exec."""
    mngr_binary = _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope(_ENCODED_ZIP + "\n"))

    _collect(tmp_path, root_concurrency_group)

    assert _read_invocations(mngr_binary, ".outer.log") == []


def test_the_mirror_exec_carries_the_outer_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The flags reach the real invocation, not just the argv builder."""
    mngr_binary = _write_fake_mngr(tmp_path, monkeypatch, envelope=exec_json_envelope(_ENCODED_ZIP + "\n"))

    _collect(tmp_path, root_concurrency_group, latchkey_plugin_data_dir=tmp_path / "latchkey-plugin")

    outer_invocations = _read_invocations(mngr_binary, ".outer.log")
    assert outer_invocations[:2] == ["exec", str(_WORKSPACE_AGENT_ID)]
    assert "--outer" in outer_invocations
    assert "--no-start" in outer_invocations
