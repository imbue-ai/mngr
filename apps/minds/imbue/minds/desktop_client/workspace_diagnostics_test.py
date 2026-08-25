"""Unit coverage for the bug report's workspace diagnostics collection.

Exercises the host half of ``workspace_diagnostics``: the sentinel/payload
parsing that decides whether the exec ever reached the resident collector, the
closed set of omission reasons every failure mode lands on, the tiny
probe-and-run ``mngr exec`` argv, and the per-collection staged files a report
is attached from by exact path.

The exec boundary is driven through a fake ``mngr`` executable on disk -- the
same stub technique ``workspace_recovery_test`` uses -- injected through the
existing ``mngr_binary`` parameter. No real workspace, collector, secret
scanner, or secret is involved anywhere: an in-container scan verdict reaches
the host only as a reason code in the canned payload, so ``secrets_found`` is
exercised with ordinary text.

The collector hands everything back as ONE base64 zip, so the archives these
tests feed the host are real zips built the collector's way (one JSONL member
per chat, plus the logs member) -- what the host must do with them (decode,
stage under ``.zip``, survive a garbled payload) cannot be exercised with a
text stand-in. The console never touches the exec: the shell's captured tail is
staged app-side, unscanned, by ``collect_workspace_diagnostics`` itself.
"""

import base64
import io
import json
import os
import time
import zipfile
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.event_utils import ReadOnlyEvent
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.desktop_client.workspace_diagnostics import COLLECTOR_MISSING_SENTINEL
from imbue.minds.desktop_client.workspace_diagnostics import CONSOLE_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import DIAGNOSTICS_CONTRACT_VERSION
from imbue.minds.desktop_client.workspace_diagnostics import DIAGNOSTICS_SENTINEL
from imbue.minds.desktop_client.workspace_diagnostics import STAGED_FILENAME_PREFIX
from imbue.minds.desktop_client.workspace_diagnostics import STAGED_FILENAME_SUFFIX_BY_KEY
from imbue.minds.desktop_client.workspace_diagnostics import STALE_STAGED_FILE_MAX_AGE_SECONDS
from imbue.minds.desktop_client.workspace_diagnostics import TRANSCRIPT_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_COLLECTOR_PATH
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_LOGS_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_ZIP_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceDiagnosticsOmissionReason
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceDiagnosticsResult
from imbue.minds.desktop_client.workspace_diagnostics import _SCAN_TIMEOUT_FRACTION_OF_BUDGET
from imbue.minds.desktop_client.workspace_diagnostics import build_diagnostics_argv
from imbue.minds.desktop_client.workspace_diagnostics import build_diagnostics_shell_command
from imbue.minds.desktop_client.workspace_diagnostics import build_staged_diagnostics_filename
from imbue.minds.desktop_client.workspace_diagnostics import collect_workspace_diagnostics
from imbue.minds.desktop_client.workspace_diagnostics import is_collector_missing
from imbue.minds.desktop_client.workspace_diagnostics import parse_diagnostics_payload
from imbue.minds.desktop_client.workspace_diagnostics import sweep_stale_staged_diagnostics_files
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState

_WORKSPACE_AGENT_ID: AgentId = AgentId("agent-" + "0" * 31 + "3")

# Long enough to hold the whole probe-and-run shell command (a few hundred
# bytes) so a test can assert on it, while still skipping anything
# pathological.
_MAX_RECORDED_ARG_LENGTH: Final[int] = 2048

# The command must stay far below Linux's 128KB MAX_ARG_STRLEN cap on a SINGLE
# argv string. The retired design base64-inlined the collector, a secret
# scanner, its config, and the console into this one string, and a chatty
# console pushed it over the cliff -- the container's bash answered "Argument
# list too long" and the ENTIRE collection silently failed. The resident
# collector exists so nothing bulky ever rides in the argv again; this bound is
# what keeps anyone from reintroducing an inline payload.
_MAX_COMMAND_BYTES: Final[int] = 4 * 1024

_LOGS_TEXT: Final[str] = "supervisord status\nsystem_interface RUNNING\n"
_TRANSCRIPT_TEXT: Final[str] = '{"type": "user", "text": "hello"}\n'
_CONSOLE_TAIL_TEXT: Final[str] = "[renderer] chat frame sent\n"

# Members inside the workspace zip, named as the collector names them: the
# logs file, and one JSONL per chat carrying the owning agent id and harness.
_LOGS_MEMBER_NAME: Final[str] = "workspace-logs.log"
_TRANSCRIPT_MEMBER_NAME: Final[str] = f"chats/{_WORKSPACE_AGENT_ID}-claude.jsonl"
# Fixed so an archive built twice from the same members is byte-identical, which
# is what lets a staged file be compared against the bytes that were sent.
_ZIP_MEMBER_DATE_TIME: Final[tuple[int, int, int, int, int, int]] = (2026, 1, 2, 3, 4, 5)

_DEFAULT_ZIP_MEMBERS: Final[Mapping[str, str]] = {
    _LOGS_MEMBER_NAME: _LOGS_TEXT,
    _TRANSCRIPT_MEMBER_NAME: _TRANSCRIPT_TEXT,
}


def _diagnostics_stdout(
    zip_members: Mapping[str, str] | None = None,
    omissions: Mapping[str, str] | None = None,
    preamble: str = "",
    encoded_zip: str | None = None,
) -> str:
    """Build a collection stdout: optional pre-sentinel noise, the READY sentinel, one JSON line.

    ``zip_members`` builds a real zip for the payload's ``zip``; ``encoded_zip``
    overrides it verbatim (for garbled-base64 cases); with neither, the ``zip``
    member is absent, as the contract requires when nothing was collected.
    """
    payload: dict[str, object] = {
        "contract_version": DIAGNOSTICS_CONTRACT_VERSION,
        "omissions": dict(omissions or {}),
    }
    if encoded_zip is not None:
        payload["zip"] = encoded_zip
    elif zip_members is not None:
        payload["zip"] = _encoded_zip(zip_members)
    else:
        # No ``zip`` member at all: the contract's shape when nothing was collected.
        pass
    return preamble + DIAGNOSTICS_SENTINEL + "\n" + json.dumps(payload) + "\n"


def _zip_bytes(members: Mapping[str, str]) -> bytes:
    """A workspace archive shaped like the collector's: one member per collected file."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=_ZIP_MEMBER_DATE_TIME), content)
    return buffer.getvalue()


def _encoded_zip(members: Mapping[str, str]) -> str:
    """The archive as the payload carries it: base64, since JSON holds no bytes."""
    return base64.b64encode(_zip_bytes(members)).decode("ascii")


def _write_fake_mngr(tmp_path: Path, stdout_text: str = "", exit_code: int = 0) -> str:
    """Write an executable stub that stands in for the ``mngr`` binary.

    It prints ``stdout_text`` verbatim -- read from a sibling file, so a JSON
    payload needs no shell quoting -- and appends every argument it was called
    with to a ``<script>.log`` sibling, so a test can assert both what the real
    invocation carried and that no invocation happened at all.
    """
    stdout_path = tmp_path / "fake_mngr_stdout.txt"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    script = tmp_path / "fake_mngr"
    script.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        f'  if [ ${{#arg}} -le {_MAX_RECORDED_ARG_LENGTH} ]; then printf "%s\\n" "$arg" >> "$0.log"; fi\n'
        "done\n"
        f'cat "{stdout_path}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def _read_fake_mngr_invocations(mngr_binary: str) -> list[str]:
    """Return the recorded arguments for a ``_write_fake_mngr`` stub (empty if never invoked)."""
    log_path = Path(mngr_binary + ".log")
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf-8").splitlines()


def _recorded_shell_command(mngr_binary: str) -> str:
    """The probe-and-run shell command a fake-mngr invocation carried."""
    (command,) = [arg for arg in _read_fake_mngr_invocations(mngr_binary) if DIAGNOSTICS_SENTINEL in arg]
    return command


def _logs_dir(tmp_path: Path) -> Path:
    """The minds logs dir a collection stages its attachments into, created eagerly."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    return logs_dir


def _staged_files(logs_dir: Path) -> list[Path]:
    """Every staged bug-report file in the dir, whichever collection wrote it.

    Covers every suffix staging can produce (the console's ``.log`` and the
    workspace archive's ``.zip``), so an assertion about what a collection left
    behind cannot go blind to one of them.
    """
    suffixes = set(STAGED_FILENAME_SUFFIX_BY_KEY.values())
    return sorted(path for path in logs_dir.glob(f"{STAGED_FILENAME_PREFIX}*") if path.suffix in suffixes)


def _make_stale(path: Path) -> None:
    """Backdate a file past the sweep's stale age."""
    stale_time = time.time() - STALE_STAGED_FILE_MAX_AGE_SECONDS - 60.0
    os.utime(path, (stale_time, stale_time))


def _collect(
    tmp_path: Path,
    concurrency_group: ConcurrencyGroup,
    mngr_binary: str,
    include_logs: bool = True,
    include_transcript: bool = True,
    host_state: HostState | None = None,
    console_text: str | None = None,
) -> WorkspaceDiagnosticsResult:
    """Run a collection against a fake mngr, defaulting to both boxes checked.

    ``console_text`` defaults to None -- the shell never captured a tail -- so a
    test that wants a console staged must pass one explicitly.
    """
    return collect_workspace_diagnostics(
        _WORKSPACE_AGENT_ID,
        include_logs=include_logs,
        include_transcript=include_transcript,
        logs_dir=_logs_dir(tmp_path),
        host_state=host_state,
        mngr_binary=mngr_binary,
        mngr_host_dir=tmp_path / "mngr_host_dir",
        concurrency_group=concurrency_group,
        console_text=console_text,
    )


class _TimedOutConcurrencyGroup(ConcurrencyGroup):
    """A group whose process runs always report as killed by their own timeout.

    Injected through the ``concurrency_group`` parameter collection already
    takes. The collection budget is ``WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS``,
    far too long for a unit test to wait out with a sleeping stub, so the
    timed-out result is produced at the boundary instead.
    """

    def run_process_to_completion(
        self,
        command: Sequence[str],
        timeout: float | None = None,
        is_checked_after: bool = True,
        on_output: Callable[[str, bool], None] | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        shutdown_event: ReadOnlyEvent | None = None,
        name: str | None = None,
    ) -> FinishedProcess:
        return FinishedProcess(
            command=tuple(command),
            returncode=None,
            stdout="",
            stderr="",
            is_timed_out=True,
            is_output_already_logged=False,
        )


# --- sentinel / payload parsing -------------------------------------------


def test_parse_payload_reads_the_zip_and_omissions_the_collector_returned() -> None:
    encoded = _encoded_zip(_DEFAULT_ZIP_MEMBERS)
    payload = parse_diagnostics_payload(
        _diagnostics_stdout(encoded_zip=encoded, omissions={TRANSCRIPT_ATTACHMENT_KEY: "no_chat_transcript"})
    )
    assert payload is not None
    assert payload.contract_version == DIAGNOSTICS_CONTRACT_VERSION
    assert payload.zip_base64 == encoded
    assert payload.omissions == {TRANSCRIPT_ATTACHMENT_KEY: "no_chat_transcript"}


def test_parse_payload_reads_an_absent_zip_as_none_rather_than_empty() -> None:
    """The contract says ``zip`` is ABSENT (not empty) when nothing was collected."""
    payload = parse_diagnostics_payload(
        _diagnostics_stdout(omissions={WORKSPACE_LOGS_ATTACHMENT_KEY: "secrets_found"})
    )
    assert payload is not None
    assert payload.zip_base64 is None


def test_parse_payload_is_none_when_the_ready_sentinel_never_landed() -> None:
    """No READY sentinel means the collector never ran, so there is nothing to interpret.

    All three shapes are plumbing failures: no stdout at all, empty stdout, and
    stdout that carries only mngr's own error output.
    """
    assert parse_diagnostics_payload(None) is None
    assert parse_diagnostics_payload("") is None
    assert parse_diagnostics_payload("Error: no such agent\n") is None


def test_the_collector_missing_sentinel_is_its_own_answer_not_a_payload() -> None:
    """The probe prints exactly one sentinel, and each one means something different.

    COLLECTOR-MISSING is an answer (the workspace predates the collector), not a
    payload to parse -- and a READY stdout must never read as missing.
    """
    missing_stdout = COLLECTOR_MISSING_SENTINEL + "\n"
    assert is_collector_missing(missing_stdout)
    assert parse_diagnostics_payload(missing_stdout) is None

    ready_stdout = _diagnostics_stdout(zip_members=_DEFAULT_ZIP_MEMBERS)
    assert not is_collector_missing(ready_stdout)
    assert not is_collector_missing(None)
    assert not is_collector_missing("Error: no such agent\n")


def test_parse_payload_skips_preamble_noise_before_the_sentinel() -> None:
    """Only what follows the sentinel is the payload.

    ``--quiet`` is meant to keep mngr's chatter out of stdout, but the parse must
    not depend on that: a decoy JSON line printed before the sentinel (a warning,
    a progress record) must be ignored rather than parsed as the payload.
    """
    preamble = (
        'connecting to agent...\n{"contract_version": 99, "zip": "decoy", "omissions": {}}\nwarning: slow host\n'
    )
    encoded = _encoded_zip(_DEFAULT_ZIP_MEMBERS)
    payload = parse_diagnostics_payload(_diagnostics_stdout(encoded_zip=encoded, preamble=preamble))
    assert payload is not None
    assert payload.zip_base64 == encoded


def test_parse_payload_returns_an_empty_payload_when_the_sentinel_stands_alone() -> None:
    """A sentinel with nothing after it is an observation, not a plumbing failure.

    The exec demonstrably reached the container, so this must be a payload (which
    the caller resolves per attachment) rather than the None that means the exec
    never got inside.
    """
    payload = parse_diagnostics_payload(DIAGNOSTICS_SENTINEL + "\n")
    assert payload is not None
    assert payload.zip_base64 is None
    assert payload.omissions == {}


@pytest.mark.parametrize("json_line", ["not json at all", "[1, 2, 3]", '"a bare string"'])
def test_parse_payload_returns_an_empty_payload_for_an_unusable_json_line(json_line: str) -> None:
    """A garbled line after the sentinel still means the exec got inside.

    Same distinction as the bare sentinel: the collector ran, so its output being
    unusable is the collector's problem, not the plumbing's.
    """
    payload = parse_diagnostics_payload(DIAGNOSTICS_SENTINEL + "\n" + json_line + "\n")
    assert payload is not None
    assert payload.zip_base64 is None
    assert payload.omissions == {}


def test_parse_payload_drops_payload_members_of_the_wrong_shape() -> None:
    """The payload crosses a process boundary, so its shape is checked, not trusted.

    A non-string ``zip`` and non-string omission values are dropped rather than
    carried as far as the decoder; a non-integer contract version reads as none.
    """
    stdout = (
        DIAGNOSTICS_SENTINEL
        + "\n"
        + json.dumps(
            {
                "contract_version": "one",
                "zip": ["not", "a", "string"],
                "omissions": {"workspace_logs": 17, "transcript": "no_chat_transcript"},
            }
        )
        + "\n"
    )
    payload = parse_diagnostics_payload(stdout)
    assert payload is not None
    assert payload.contract_version is None
    assert payload.zip_base64 is None
    assert payload.omissions == {TRANSCRIPT_ATTACHMENT_KEY: "no_chat_transcript"}


# --- the mngr exec argv ---------------------------------------------------


def test_build_diagnostics_argv_carries_no_start_quiet_and_the_timeout() -> None:
    """``--no-start`` keeps a bug report from booting a stopped workspace; ``--quiet``
    keeps mngr's chatter out of the sentinel-delimited stdout; ``--timeout`` caps the
    in-container work at the same budget the outer subprocess is given."""
    argv = build_diagnostics_argv("/usr/local/bin/mngr", _WORKSPACE_AGENT_ID, True, True)
    assert argv[:3] == ["/usr/local/bin/mngr", "exec", str(_WORKSPACE_AGENT_ID)]
    assert "--no-start" in argv
    assert "--quiet" in argv
    assert argv[argv.index("--timeout") + 1] == str(int(WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS))


def test_build_diagnostics_argv_threads_a_custom_budget_through() -> None:
    """Only tests pass a non-default budget (a shared CI sandbox is slower than the
    user machines the production budget is policy for), but when one is passed it
    must reach the exec, or the sandbox tests would silently time out at the
    production budget anyway."""
    argv = build_diagnostics_argv("/usr/local/bin/mngr", _WORKSPACE_AGENT_ID, True, True, timeout_seconds=120.0)
    assert argv[argv.index("--timeout") + 1] == "120"


def test_the_scan_timeout_is_the_same_fraction_of_any_budget() -> None:
    """The in-container scan gets a fixed share of whatever budget collection runs under, stretching
    with a longer one so the scan does not starve at its production share while the exec idles.

    Derived from the constants rather than written as literals: the production budget is a tuning
    knob that has already moved twice, and pinning its arithmetic here would make a future change to
    it look like a broken test rather than the deliberate retune it is."""
    production_share = int(WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS * _SCAN_TIMEOUT_FRACTION_OF_BUDGET)
    assert f"--scan-timeout={production_share}" in build_diagnostics_shell_command(True, True)

    doubled = WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS * 2
    sandbox_command = build_diagnostics_shell_command(True, True, timeout_seconds=doubled)
    assert f"--scan-timeout={int(doubled * _SCAN_TIMEOUT_FRACTION_OF_BUDGET)}" in sandbox_command
    assert production_share > 0, "the scan must get a usable share of the budget"


@pytest.mark.parametrize(
    ("include_logs", "include_transcript", "expected_flags"),
    [
        (True, True, ("--logs", "--transcript")),
        (True, False, ("--logs",)),
        (False, True, ("--transcript",)),
    ],
)
def test_the_shell_command_asks_for_only_the_checked_attachments(
    include_logs: bool, include_transcript: bool, expected_flags: tuple[str, ...]
) -> None:
    """An unchecked box means the collector is never asked for that content at all."""
    command = build_diagnostics_shell_command(include_logs, include_transcript)
    collector_invocation = command.split("python3", 1)[1]
    assert tuple(flag for flag in ("--logs", "--transcript") if flag in collector_invocation) == expected_flags


def test_the_shell_command_probes_the_resident_collector_and_answers_missing() -> None:
    """The command runs the template's own collector, and a workspace without one
    answers with the MISSING sentinel instead of a shell error the parse cannot
    tell from any other failed exec."""
    command = build_diagnostics_shell_command(True, True)
    assert WORKSPACE_COLLECTOR_PATH in command
    assert COLLECTOR_MISSING_SENTINEL in command
    assert command.index(DIAGNOSTICS_SENTINEL) < command.index("python3")


def test_the_command_is_tiny_because_nothing_travels_in_it() -> None:
    """The command must stay a few hundred bytes -- no inline payloads, ever.

    The retired design base64-inlined the collector script, a secret scanner,
    its config, and the gzipped console into this ONE argv string, and Linux
    caps a single argv string at 128KB (MAX_ARG_STRLEN): an oversized console
    made the container's bash refuse the whole command, silently costing the
    report every attachment. The bound is far below that cliff so any future
    inline payload fails this test long before it can fail a user's report.
    """
    for include_logs, include_transcript in ((True, True), (True, False), (False, True)):
        command = build_diagnostics_shell_command(include_logs, include_transcript)
        assert len(command.encode("utf-8")) < _MAX_COMMAND_BYTES


def test_collection_invokes_mngr_exec_with_no_start_quiet_and_the_timeout(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The flags reach the real invocation, not just the argv builder."""
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout(zip_members=_DEFAULT_ZIP_MEMBERS))
    _collect(tmp_path, root_concurrency_group, mngr_binary)

    invocations = _read_fake_mngr_invocations(mngr_binary)
    assert invocations[:2] == ["exec", str(_WORKSPACE_AGENT_ID)]
    assert "--no-start" in invocations
    assert "--quiet" in invocations
    assert invocations[invocations.index("--timeout") + 1] == str(int(WORKSPACE_DIAGNOSTICS_TIMEOUT_SECONDS))


# --- staged files ---------------------------------------------------------


def test_the_staged_suffix_is_chosen_per_file_rather_than_shared() -> None:
    """The workspace content stages as an archive; the console stays a log.

    The workspace zip holds one member per collected file, so it must land under
    ``.zip``: that is what tells a reader (and the upload, which leaves an
    already-compressed attachment ungzipped) that it is an archive. A single
    shared suffix would silently name one of the two kinds wrongly.
    """
    assert set(STAGED_FILENAME_SUFFIX_BY_KEY) == {WORKSPACE_ZIP_ATTACHMENT_KEY, CONSOLE_ATTACHMENT_KEY}
    assert build_staged_diagnostics_filename(WORKSPACE_ZIP_ATTACHMENT_KEY, "abcd") == "bug-report-abcd-workspace.zip"
    assert build_staged_diagnostics_filename(CONSOLE_ATTACHMENT_KEY, "abcd") == "bug-report-abcd-console.log"


def test_the_sweep_removes_old_staged_files_and_leaves_recent_ones(tmp_path: Path) -> None:
    """Only files too old for any upload to still want them are removed.

    A recent staged file may be the one a background upload is reading right
    now, so the sweep bounds disk without touching it. Unrelated files in the
    logs dir are none of the sweep's business either.
    """
    logs_dir = _logs_dir(tmp_path)
    old_staged = logs_dir / build_staged_diagnostics_filename(CONSOLE_ATTACHMENT_KEY, "aaaa")
    fresh_staged = logs_dir / build_staged_diagnostics_filename(CONSOLE_ATTACHMENT_KEY, "bbbb")
    other_log = logs_dir / "minds.log"
    for path in (old_staged, fresh_staged, other_log):
        path.write_text("staged content", encoding="utf-8")
    _make_stale(old_staged)
    _make_stale(other_log)

    sweep_stale_staged_diagnostics_files(logs_dir)

    assert not old_staged.exists()
    assert fresh_staged.exists()
    assert other_log.exists()


def test_the_sweep_removes_a_stale_workspace_archive_too(tmp_path: Path) -> None:
    """The sweep has to cover every suffix staging produces, not just ``.log``.

    A sweep that recognized only one would let every workspace archive accumulate
    on disk forever -- exactly the growth the sweep exists to bound, and
    invisible because the console files beside them would keep disappearing on
    schedule.
    """
    logs_dir = _logs_dir(tmp_path)
    stale_archive = logs_dir / build_staged_diagnostics_filename(WORKSPACE_ZIP_ATTACHMENT_KEY, "aaaa")
    fresh_archive = logs_dir / build_staged_diagnostics_filename(WORKSPACE_ZIP_ATTACHMENT_KEY, "bbbb")
    for path in (stale_archive, fresh_archive):
        path.write_bytes(_zip_bytes(_DEFAULT_ZIP_MEMBERS))
    _make_stale(stale_archive)
    # Without this the test would pass just as well against a single-suffix
    # sweep, which is the regression it exists to catch.
    assert stale_archive.suffix == ".zip"

    sweep_stale_staged_diagnostics_files(logs_dir)

    assert not stale_archive.exists()
    assert fresh_archive.exists()


def test_the_sweep_tolerates_a_logs_dir_it_cannot_read(tmp_path: Path) -> None:
    """It runs on the way into every collection, so it can never be what fails one.

    A logs dir that does not exist yet (nothing has been written there) and a
    path that is not a directory at all both resolve to sweeping nothing.
    """
    sweep_stale_staged_diagnostics_files(tmp_path / "not-created-yet")

    not_a_directory = tmp_path / "a-file"
    not_a_directory.write_text("", encoding="utf-8")
    sweep_stale_staged_diagnostics_files(not_a_directory)


def test_collection_stages_the_returned_zip_under_this_collections_filename(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The zip stages as the bytes its base64 decodes to, and opens as a real archive.

    The point of the archive is that a reader can open it and find the logs and
    each conversation as its own member, so the staged file is read back through
    ``zipfile`` -- members, order, and contents. Both requested content types
    rode in the one file, so nothing is omitted but the console.
    """
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout(zip_members=_DEFAULT_ZIP_MEMBERS))
    result = _collect(tmp_path, root_concurrency_group, mngr_binary)

    staged_zip = result.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]
    assert staged_zip.read_bytes() == _zip_bytes(_DEFAULT_ZIP_MEMBERS)
    assert staged_zip.name.endswith("-workspace.zip")
    assert _staged_files(_logs_dir(tmp_path)) == [staged_zip]
    with zipfile.ZipFile(staged_zip) as archive:
        assert archive.namelist() == [_LOGS_MEMBER_NAME, _TRANSCRIPT_MEMBER_NAME]
        assert {name: archive.read(name).decode("utf-8") for name in archive.namelist()} == dict(_DEFAULT_ZIP_MEMBERS)
    # No tail was captured (the _collect default), so the console -- requested
    # alongside the logs -- reports the host-side no_console_output reason.
    assert result.attachment_omissions == {
        CONSOLE_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT
    }


def test_a_console_and_a_zip_stage_side_by_side_under_one_slug(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """One collection, one slug: the two staged files differ only in their stem and suffix."""
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout(zip_members=_DEFAULT_ZIP_MEMBERS))

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, console_text=_CONSOLE_TAIL_TEXT)

    staged_zip = result.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]
    staged_console = result.staged_paths[CONSOLE_ATTACHMENT_KEY]
    assert staged_console.read_text(encoding="utf-8") == _CONSOLE_TAIL_TEXT
    assert _staged_files(_logs_dir(tmp_path)) == sorted([staged_zip, staged_console])
    assert staged_zip.name.removesuffix("workspace.zip") == staged_console.name.removesuffix("console.log")
    assert result.attachment_omissions == {}


# Garbled (a character base64 has no digit for), truncated (a payload cut
# mid-quantum, so its padding no longer adds up), and non-ASCII -- the ways a
# value can reach the host unusable. The last is its own case because it fails
# before base64 validation even begins, raising a plain ValueError rather than
# the binascii.Error the other two raise.
@pytest.mark.parametrize("encoded", ["not base64 at all !!", "aGVsbG8", "UEsDBBé"])
def test_a_zip_that_is_not_valid_base64_is_omitted_rather_than_raised(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, encoded: str
) -> None:
    """A corrupt payload costs its attachments, never the report.

    The zip is base64 across a process boundary, so a garbled, truncated, or
    non-ASCII value is possible; decoding one raises a ``ValueError``
    (``binascii.Error`` is one), which is no ``OSError`` and would otherwise
    escape collection and take down a report that was only ever asking for
    attachments. Both content types the zip was carrying land on the same
    omission a failed exec does, with no half-written archive left behind --
    and the console, which never rode the exec, still attaches.
    """
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout(encoded_zip=encoded))

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, console_text=_CONSOLE_TAIL_TEXT)

    staged_console = result.staged_paths[CONSOLE_ATTACHMENT_KEY]
    assert set(result.staged_paths) == {CONSOLE_ATTACHMENT_KEY}
    assert _staged_files(_logs_dir(tmp_path)) == [staged_console]
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
    }


def test_two_collections_stage_side_by_side_instead_of_overwriting_each_other(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Concurrent reports must not clobber one another's staged files.

    Reports submit immediately and finish collecting and uploading in the
    background, so two collections can be in flight over the same logs dir. Each
    gets its own slug, so both files survive with their own content and either
    upload can still read the file it was handed.
    """
    logs_dir = _logs_dir(tmp_path)
    # A stub per collection (each writes its own canned stdout beside itself), so
    # the two collections return distinguishable content.
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_binary = _write_fake_mngr(first_dir, stdout_text=_diagnostics_stdout(zip_members=_DEFAULT_ZIP_MEMBERS))
    second_members = {_TRANSCRIPT_MEMBER_NAME: _TRANSCRIPT_TEXT + '{"type": "user", "text": "and again"}\n'}
    second_binary = _write_fake_mngr(second_dir, stdout_text=_diagnostics_stdout(zip_members=second_members))

    first = _collect(tmp_path, root_concurrency_group, first_binary, include_logs=False)
    second = _collect(tmp_path, root_concurrency_group, second_binary, include_logs=False)

    first_staged = first.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]
    second_staged = second.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]
    assert first_staged != second_staged
    assert first_staged.read_bytes() == _zip_bytes(_DEFAULT_ZIP_MEMBERS)
    assert second_staged.read_bytes() == _zip_bytes(second_members)
    assert _staged_files(logs_dir) == sorted([first_staged, second_staged])


def test_collection_leaves_an_earlier_reports_staged_files_where_they_are(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A recent file from an earlier report is left alone -- and cannot ride along.

    Attachments are one-shot, by exact path, so only what this collection staged
    can reach this report; deleting the earlier report's files would instead
    race the background upload still reading them.
    """
    logs_dir = _logs_dir(tmp_path)
    earlier_members = {_TRANSCRIPT_MEMBER_NAME: "the previous report's transcript"}
    earlier_zip = logs_dir / build_staged_diagnostics_filename(WORKSPACE_ZIP_ATTACHMENT_KEY, "earlier")
    earlier_zip.write_bytes(_zip_bytes(earlier_members))
    mngr_binary = _write_fake_mngr(
        tmp_path, stdout_text=_diagnostics_stdout(zip_members={_LOGS_MEMBER_NAME: _LOGS_TEXT})
    )

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, include_transcript=False)

    assert earlier_zip.read_bytes() == _zip_bytes(earlier_members)
    assert set(result.staged_paths) == {WORKSPACE_ZIP_ATTACHMENT_KEY}
    assert result.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY] != earlier_zip


def test_collection_sweeps_a_stale_staged_file_on_its_way_in(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Unique names mean nothing deletes staged files, so collection bounds the disk."""
    logs_dir = _logs_dir(tmp_path)
    ancient = logs_dir / build_staged_diagnostics_filename(CONSOLE_ATTACHMENT_KEY, "ancient")
    ancient.write_text("a report from last week", encoding="utf-8")
    _make_stale(ancient)
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout(zip_members=_DEFAULT_ZIP_MEMBERS))

    result = _collect(tmp_path, root_concurrency_group, mngr_binary)

    assert not ancient.exists()
    assert _staged_files(logs_dir) == [result.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]]


# --- omission reasons -----------------------------------------------------


def test_the_omission_reasons_are_the_documented_closed_set() -> None:
    """The values are a wire format: Sentry's ``attachment_omissions`` extra carries
    them verbatim, so the set is pinned rather than free to drift."""
    assert {reason.value for reason in WorkspaceDiagnosticsOmissionReason} == {
        "not_requested",
        "host_stopped",
        "exec_failed",
        "exec_timeout",
        "collector_unavailable",
        "scanner_unavailable",
        "secrets_found",
        "no_chat_transcript",
        "no_console_output",
    }


def test_unchecked_boxes_report_not_requested_and_spawn_nothing(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Nothing requested means no exec at all, and nothing staged for this report.

    The console rides on the logs checkbox, so with the box unticked it is
    not_requested even though the shell did capture a tail -- a captured tail is
    never attached to a report that asked for no logs. An earlier report's
    recent file is left where it is: it belongs to that report's upload, and
    cannot reach this one.
    """
    logs_dir = _logs_dir(tmp_path)
    earlier = logs_dir / build_staged_diagnostics_filename(CONSOLE_ATTACHMENT_KEY, "earlier")
    earlier.write_text("an earlier report's console", encoding="utf-8")
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout())

    result = _collect(
        tmp_path,
        root_concurrency_group,
        mngr_binary,
        include_logs=False,
        include_transcript=False,
        console_text=_CONSOLE_TAIL_TEXT,
    )

    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert result.staged_paths == {}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED,
        CONSOLE_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED,
    }
    assert _staged_files(logs_dir) == [earlier]


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
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, host_state: HostState
) -> None:
    """A report filed from a stopped workspace submits immediately.

    An ``mngr exec --no-start`` against a host that is not running could only wait
    out the collection budget, so the passive host state short-circuits it: the
    stub must never be invoked. The console does not need the workspace at all
    any more, so the captured tail still stages -- see the dedicated test below.
    """
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout())

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, host_state=host_state)

    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert result.staged_paths == {}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.HOST_STOPPED,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.HOST_STOPPED,
        CONSOLE_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT,
    }


def test_the_console_stages_without_any_exec_when_the_host_is_down(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The console is app-side output: no workspace is needed to attach it.

    With the host stopped nothing spawns, the workspace content reports
    host_stopped -- and the captured tail still stages, unscanned, staged by
    this module alone. Under the old design the console travelled into the
    container for scanning and a stopped host cost the report its console too.
    """
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout())

    result = _collect(
        tmp_path, root_concurrency_group, mngr_binary, host_state=HostState.STOPPED, console_text=_CONSOLE_TAIL_TEXT
    )

    assert _read_fake_mngr_invocations(mngr_binary) == []
    staged_console = result.staged_paths[CONSOLE_ATTACHMENT_KEY]
    assert staged_console.read_text(encoding="utf-8") == _CONSOLE_TAIL_TEXT
    assert staged_console.name.endswith("-console.log")
    assert set(result.staged_paths) == {CONSOLE_ATTACHMENT_KEY}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.HOST_STOPPED,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.HOST_STOPPED,
    }


@pytest.mark.parametrize("host_state", [None, HostState.RUNNING, HostState.STARTING, HostState.UNKNOWN])
def test_a_host_that_is_not_known_to_be_down_still_attempts_collection(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, host_state: HostState | None
) -> None:
    """The short-circuit is evidence-based: an absent or non-terminal state is not
    evidence the workspace is down, so collection is attempted anyway."""
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout(zip_members=_DEFAULT_ZIP_MEMBERS))

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, host_state=host_state)

    assert _read_fake_mngr_invocations(mngr_binary)[:1] == ["exec"]
    assert set(result.staged_paths) == {WORKSPACE_ZIP_ATTACHMENT_KEY}


def test_a_workspace_without_the_collector_reports_collector_unavailable(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The MISSING sentinel means the template predates the resident collector.

    Every workspace content type the report asked for is omitted with
    ``collector_unavailable`` -- and the console, which does not depend on the
    workspace, still stages.
    """
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=COLLECTOR_MISSING_SENTINEL + "\n")

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, console_text=_CONSOLE_TAIL_TEXT)

    staged_console = result.staged_paths[CONSOLE_ATTACHMENT_KEY]
    assert staged_console.read_text(encoding="utf-8") == _CONSOLE_TAIL_TEXT
    assert set(result.staged_paths) == {CONSOLE_ATTACHMENT_KEY}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.COLLECTOR_UNAVAILABLE,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.COLLECTOR_UNAVAILABLE,
    }


def test_an_exec_that_never_reached_the_container_reports_exec_failed(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """mngr ran and exited without either sentinel: the probe never ran.

    Only the workspace content wears the failure; the console never rides the
    exec, and with no tail captured it reports its own host-side reason.
    """
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text="Error: agent is not running\n", exit_code=1)

    result = _collect(tmp_path, root_concurrency_group, mngr_binary)

    assert result.staged_paths == {}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
        CONSOLE_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT,
    }


def test_an_mngr_binary_that_cannot_launch_reports_exec_failed(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A missing binary raises at fork/exec; the report still submits, console intact."""
    result = _collect(
        tmp_path,
        root_concurrency_group,
        str(tmp_path / "definitely_not_a_real_mngr"),
        console_text=_CONSOLE_TAIL_TEXT,
    )

    assert set(result.staged_paths) == {CONSOLE_ATTACHMENT_KEY}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
    }


def test_an_exec_killed_by_the_collection_budget_reports_exec_timeout(tmp_path: Path) -> None:
    """A timeout observed nothing, which is worth telling apart from a failed exec."""
    with _TimedOutConcurrencyGroup(name="test-diagnostics-timeout") as concurrency_group:
        result = _collect(tmp_path, concurrency_group, "/usr/local/bin/mngr", console_text=_CONSOLE_TAIL_TEXT)

    assert set(result.staged_paths) == {CONSOLE_ATTACHMENT_KEY}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_TIMEOUT,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_TIMEOUT,
    }


@pytest.mark.parametrize(
    "reason",
    [
        WorkspaceDiagnosticsOmissionReason.SCANNER_UNAVAILABLE,
        WorkspaceDiagnosticsOmissionReason.SECRETS_FOUND,
        WorkspaceDiagnosticsOmissionReason.NO_CHAT_TRANSCRIPT,
    ],
)
def test_an_in_container_reason_reaches_the_report_verbatim(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, reason: WorkspaceDiagnosticsOmissionReason
) -> None:
    """The three reasons only the in-workspace collector can observe pass through unchanged.

    The scan verdict is stubbed as the reason code the collector would have
    printed, so no scanner runs and no secret exists anywhere in this test. With
    everything requested omitted, the contract sends no zip at all, and no
    surviving-content failure may overwrite the collector's own reasons.
    """
    mngr_binary = _write_fake_mngr(
        tmp_path,
        stdout_text=_diagnostics_stdout(
            omissions={WORKSPACE_LOGS_ATTACHMENT_KEY: reason.value, TRANSCRIPT_ATTACHMENT_KEY: reason.value}
        ),
    )

    result = _collect(tmp_path, root_concurrency_group, mngr_binary)

    assert result.staged_paths == {}
    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: reason,
        TRANSCRIPT_ATTACHMENT_KEY: reason,
        CONSOLE_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT,
    }


def test_a_partial_manifest_omission_still_stages_the_zip_for_the_clean_content(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """One withheld content type does not cost the other its archive.

    The collector reports ``no_chat_transcript`` for the transcript and sends
    the zip carrying only the logs member: the zip stages, the transcript's
    reason passes through verbatim, and nothing invents an outcome for the logs
    (their presence in the staged zip IS their outcome).
    """
    members = {_LOGS_MEMBER_NAME: _LOGS_TEXT}
    mngr_binary = _write_fake_mngr(
        tmp_path,
        stdout_text=_diagnostics_stdout(
            zip_members=members,
            omissions={TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CHAT_TRANSCRIPT.value},
        ),
    )

    result = _collect(tmp_path, root_concurrency_group, mngr_binary)

    staged_zip = result.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]
    assert staged_zip.read_bytes() == _zip_bytes(members)
    assert set(result.staged_paths) == {WORKSPACE_ZIP_ATTACHMENT_KEY}
    assert result.attachment_omissions == {
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CHAT_TRANSCRIPT,
        CONSOLE_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT,
    }


@pytest.mark.parametrize("omissions", [{}, {WORKSPACE_LOGS_ATTACHMENT_KEY: "something_new"}])
def test_content_missing_without_a_reason_we_understand_falls_back_to_exec_failed(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, omissions: dict[str, str]
) -> None:
    """A requested content type that came back with neither a zip nor a known reason
    is a collection failure like any other, so the report never carries a reason
    code its reader cannot interpret."""
    mngr_binary = _write_fake_mngr(tmp_path, stdout_text=_diagnostics_stdout(omissions=omissions))

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, include_transcript=False)

    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.EXEC_FAILED,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED,
        CONSOLE_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NO_CONSOLE_OUTPUT,
    }


def test_the_console_stages_unscanned_without_the_exec_ever_carrying_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """What stages is the tail the shell captured, byte for byte, scanned by nobody.

    The exec's shell command must not mention the console at all -- no flag, no
    delivery -- and the staged file must be the host-side text itself. That is
    the decided behavior: the console matches ``electron.log``/``minds.log``,
    which already upload unscanned, and routing it through the workspace is what
    used to blow the argv cap and cost reports everything.
    """
    mngr_binary = _write_fake_mngr(
        tmp_path, stdout_text=_diagnostics_stdout(zip_members={_LOGS_MEMBER_NAME: _LOGS_TEXT})
    )

    result = _collect(
        tmp_path, root_concurrency_group, mngr_binary, include_transcript=False, console_text=_CONSOLE_TAIL_TEXT
    )

    shell_command = _recorded_shell_command(mngr_binary)
    assert "console" not in shell_command
    staged_console = result.staged_paths[CONSOLE_ATTACHMENT_KEY]
    assert staged_console.read_text(encoding="utf-8") == _CONSOLE_TAIL_TEXT
    assert set(result.staged_paths) == {WORKSPACE_ZIP_ATTACHMENT_KEY, CONSOLE_ATTACHMENT_KEY}
    assert result.attachment_omissions == {TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED}


def test_every_content_type_is_either_covered_by_a_staged_file_or_explained(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The report can always say what happened to each content type.

    A content key absent from the omissions is present in a staged file: the
    console in its own log, the workspace content in the zip. No key is ever
    both explained and covered.
    """
    mngr_binary = _write_fake_mngr(
        tmp_path, stdout_text=_diagnostics_stdout(zip_members={_LOGS_MEMBER_NAME: _LOGS_TEXT})
    )

    result = _collect(tmp_path, root_concurrency_group, mngr_binary, include_transcript=False)

    all_content_keys = {WORKSPACE_LOGS_ATTACHMENT_KEY, TRANSCRIPT_ATTACHMENT_KEY, CONSOLE_ATTACHMENT_KEY}
    covered = set()
    if WORKSPACE_ZIP_ATTACHMENT_KEY in result.staged_paths:
        covered |= {WORKSPACE_LOGS_ATTACHMENT_KEY, TRANSCRIPT_ATTACHMENT_KEY} - set(result.attachment_omissions)
    if CONSOLE_ATTACHMENT_KEY in result.staged_paths:
        covered.add(CONSOLE_ATTACHMENT_KEY)
    assert covered | set(result.attachment_omissions) == all_content_keys
    assert covered & set(result.attachment_omissions) == set()
