"""Unit tests for mngr_work_lease_lib.sh.

The library is the write half of the work-lease protocol; ``imbue.mngr.hosts.work_leases`` is
the read half. What each test asserts about a lease it wrote is read back through that parser
rather than through a literal, so the two halves are pinned against each other.
"""

from __future__ import annotations

import shlex
import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.hosts.work_leases import WORK_LEASE_DIR_NAME
from imbue.mngr.hosts.work_leases import parse_work_leases
from imbue.mngr.primitives import WorkLease

_LIB: Path = Path(__file__).parent / "mngr_work_lease_lib.sh"


def _run_lib(state_dir: Path | None, body: str) -> subprocess.CompletedProcess[str]:
    """Source the library and run ``body``, with or without an agent to lease against."""
    # Unset rather than merely unexported: these tests run inside an mngr agent, whose own
    # state dir the subprocess would otherwise inherit.
    export = (
        f"export MNGR_AGENT_STATE_DIR={shlex.quote(str(state_dir))}\n"
        if state_dir is not None
        else "unset MNGR_AGENT_STATE_DIR\n"
    )
    script = f"{export}source {shlex.quote(str(_LIB))}\n{body}"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def _read_leases(state_dir: Path) -> tuple[WorkLease, ...]:
    """Every lease under the agent's lease dir, parsed by the reader that consumes them."""
    lease_dir = state_dir / WORK_LEASE_DIR_NAME
    if not lease_dir.is_dir():
        return ()
    lines = sorted(f"{path.name}\t{path.read_text().strip()}" for path in lease_dir.iterdir())
    return parse_work_leases("\n".join(lines))


def test_a_declared_lease_parses_with_a_bound_a_ttl_away(tmp_path: Path) -> None:
    declared_at = datetime.now(timezone.utc)

    result = _run_lib(tmp_path, "mngr_work_lease_declare task-abc 3600")
    assert result.returncode == 0, result.stderr

    leases = _read_leases(tmp_path)

    assert len(leases) == 1
    assert leases[0].name == "task-abc"
    assert leases[0].expires_at is not None
    assert timedelta(seconds=3595) <= leases[0].expires_at - declared_at <= timedelta(seconds=3605)


def test_a_name_is_reduced_to_the_grammar_the_reader_accepts(tmp_path: Path) -> None:
    """An id comes straight from an agent CLI, so the writer cannot assume it is safe.

    A name carrying a tab would split the reader's ``<name>\\t<bound>`` line in the wrong
    place and, in a batched read, corrupt every other agent's leases in the same scan.
    """
    _run_lib(tmp_path, "mngr_work_lease_declare 'task-a/b c\td' 3600")

    leases = _read_leases(tmp_path)

    assert len(leases) == 1
    assert leases[0].name == "task-a_b_c_d"


def test_declaring_leaves_no_staging_file_a_reader_would_count(tmp_path: Path) -> None:
    """The staging name is a dotfile because readers collect leases with ``leases/*``."""
    _run_lib(tmp_path, "mngr_work_lease_declare task-abc 3600")

    assert [path.name for path in (tmp_path / WORK_LEASE_DIR_NAME).iterdir()] == ["task-abc"]


def _is_any_lease_live(state_dir: Path) -> bool:
    """The library's verdict, read from a branch so the probe itself always exits cleanly."""
    result = _run_lib(state_dir, "if mngr_work_lease_any_live; then echo LIVE; else echo IDLE; fi")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in ("LIVE", "IDLE"), result.stdout
    return result.stdout.strip() == "LIVE"


def test_a_lease_holding_now_reads_as_live(tmp_path: Path) -> None:
    """What keeps a host awake while work outlives a turn: the turn marker is gone by then."""
    _run_lib(tmp_path, "mngr_work_lease_declare task-abc 3600")

    assert _is_any_lease_live(tmp_path) is True


def test_a_lease_past_its_deadline_does_not_hold_the_host_awake(tmp_path: Path) -> None:
    """A deadline that has passed is the whole point of having one: work that died silently
    stops counting without anyone coming back to remove its lease."""
    lease_dir = tmp_path / WORK_LEASE_DIR_NAME
    lease_dir.mkdir()
    (lease_dir / "task-expired").write_text("until:1000\n")

    assert _is_any_lease_live(tmp_path) is False


def test_one_live_lease_is_enough_whatever_else_the_dir_holds(tmp_path: Path) -> None:
    """The dir belongs to the agent, so a reader meets whatever anything else left there.

    A directory under a lease's own name is the shape the turn-end hook cannot clear, and the
    shape that aborts an awk given it as an argument -- so it must not decide the answer.
    """
    lease_dir = tmp_path / WORK_LEASE_DIR_NAME
    lease_dir.mkdir()
    (lease_dir / "task-expired").write_text("until:1000\n")
    (lease_dir / "task-unbounded").write_text("forever\n")
    (lease_dir / "task-unclearable").mkdir()
    (lease_dir / "zz-someone-elses").write_text("since:1 until:4102444800\n")

    assert _is_any_lease_live(tmp_path) is True


def test_nothing_outstanding_reads_as_not_live(tmp_path: Path) -> None:
    assert _is_any_lease_live(tmp_path) is False

    (tmp_path / WORK_LEASE_DIR_NAME).mkdir()

    assert _is_any_lease_live(tmp_path) is False


def test_clearing_removes_only_the_named_prefixes(tmp_path: Path) -> None:
    """The lease dir belongs to the agent, so a writer clears only what it wrote.

    Someone else's lease states its own bound and expires on that; removing it would report
    their work as finished.
    """
    _run_lib(
        tmp_path,
        "mngr_work_lease_declare task-abc 3600\n"
        "mngr_work_lease_declare watch-xyz 3600\n"
        "mngr_work_lease_declare other-build 3600\n"
        "mngr_work_lease_clear task- watch-",
    )

    assert [lease.name for lease in _read_leases(tmp_path)] == ["other-build"]


def test_clearing_a_lease_dir_that_was_never_created_is_not_an_error(tmp_path: Path) -> None:
    result = _run_lib(tmp_path, "mngr_work_lease_clear task-")

    assert result.returncode == 0, result.stderr
    assert _read_leases(tmp_path) == ()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("mngr_work_lease_declare task-abc 3600", id="declare"),
        pytest.param("mngr_work_lease_clear task-", id="clear"),
        pytest.param("mngr_work_lease_dir", id="dir"),
    ],
)
def test_every_entry_point_is_a_no_op_without_an_agent_to_lease_against(body: str) -> None:
    """A hook run outside an mngr agent must not fail on account of the leases."""
    result = _run_lib(None, body)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_declaring_an_unnamed_lease_is_a_no_op(tmp_path: Path) -> None:
    """A lease nothing names could never be matched to the work it stands for."""
    result = _run_lib(tmp_path, "mngr_work_lease_declare '' 3600")

    assert result.returncode == 0, result.stderr
    assert _read_leases(tmp_path) == ()


def test_the_library_leaves_strict_mode_to_the_script_that_sources_it(tmp_path: Path) -> None:
    """Hooks source this while already running under ``set -e`` and must keep failing their way.

    Declaring ``set -euo pipefail`` here would switch options on behind the sourcing script,
    where an unset variable it reads deliberately would start killing the turn-end signal.
    """
    before = subprocess.run(["bash", "-c", "set +e; echo $-"], capture_output=True, text=True, timeout=30)
    after = _run_lib(tmp_path, "echo $-")

    assert "e" not in before.stdout.strip()
    assert "e" not in after.stdout.strip()
    assert "u" not in after.stdout.strip()
