from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.utils.ssh_transport import is_closed_ssh_connection_stderr
from imbue.mngr.utils.ssh_transport import run_process_retrying_closed_ssh_connections
from imbue.mngr.utils.testing import make_command_failing_n_times


# The stderr of the MIND-478 failures, verbatim from the CI logs that recorded them.
@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            "Connection closed by 44.202.76.70 port 39649\n"
            "rsync: connection unexpectedly closed (0 bytes received so far) [sender]\n"
            "rsync error: unexplained error (code 255) at io.c(285) [sender=3.5.0]",
            True,
        ),
        (
            "kex_exchange_identification: read: Connection reset by peer\nConnection reset by 44.212.221.149 port 44141",
            True,
        ),
        ("rsync: connection unexpectedly closed (0 bytes received so far) [sender]", True),
        ("ssh: connect to host r448.modal.host port 39649: Connection refused", False),
        ("Host key verification failed.", False),
        ('rsync: [sender] change_dir "/nope" failed: No such file or directory (2)', False),
        ("fatal: Could not read from remote repository.", False),
        ("", False),
    ],
    ids=[
        "rsync-closed-before-any-bytes",
        "git-push-kex-reset",
        "rsync-zero-bytes-alone",
        "refused",
        "host-key-mismatch",
        "missing-source-dir",
        "git-generic-remote-failure",
        "empty",
    ],
)
def test_is_closed_ssh_connection_stderr_matches_only_a_peer_that_hung_up(stderr: str, expected: bool) -> None:
    """Only a peer closing the connection counts, so everything else keeps failing fast.

    A refusal and a host-key mismatch are excluded so a host that is genuinely down or
    mispinned fails on the first attempt, and git's generic "could not read from remote
    repository" is excluded because it accompanies every push failure, retryable or not.
    """
    assert is_closed_ssh_connection_stderr(stderr) is expected


def test_a_transfer_whose_ssh_connection_the_peer_closed_runs_again_and_succeeds(
    active_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """The MIND-478 pin: a closed connection costs one more attempt, not the whole transfer.

    A fresh Modal sandbox closes the brand-new connection of the first bulk transfer
    seconds after a readiness probe has already authenticated through the same endpoint.
    """
    command, attempt_file = make_command_failing_n_times(tmp_path, 1, "Connection closed by 203.0.113.7 port 39649")

    finished = run_process_retrying_closed_ssh_connections(
        active_concurrency_group, command, None, deadline_seconds=5.0, backoff_seconds=0.0
    )

    assert finished.returncode == 0
    assert attempt_file.read_text().strip() == "2"


def test_a_transfer_that_fails_on_its_own_terms_is_not_run_again(
    active_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """A command that failed for its own reasons surfaces its failure without a second attempt."""
    command, attempt_file = make_command_failing_n_times(
        tmp_path, 1, 'rsync: [sender] change_dir "/nope" failed: No such file or directory (2)'
    )

    with pytest.raises(ProcessError):
        run_process_retrying_closed_ssh_connections(
            active_concurrency_group, command, None, deadline_seconds=5.0, backoff_seconds=0.0
        )

    assert attempt_file.read_text().strip() == "1"


def test_a_connection_the_peer_never_stops_closing_surfaces_its_failure_after_the_deadline(
    active_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """A wall-clock deadline bounds the retry, so many attempts happen before the failure surfaces."""
    command, attempt_file = make_command_failing_n_times(
        tmp_path, 1_000_000, "kex_exchange_identification: read: Connection reset by peer"
    )

    with pytest.raises(ProcessError):
        run_process_retrying_closed_ssh_connections(
            active_concurrency_group, command, None, deadline_seconds=0.2, backoff_seconds=0.01
        )

    assert int(attempt_file.read_text().strip()) > 1


def test_the_command_runs_with_the_environment_it_was_handed(
    active_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """The environment reaches the command: git's transfers carry GIT_SSH_COMMAND in it."""
    script_path = tmp_path / f"print-env-{uuid4().hex}.sh"
    script_path.write_text('printf "%s" "$MNGR_SSH_TRANSPORT_PROBE_VALUE"\n')
    probe_value = uuid4().hex

    finished = run_process_retrying_closed_ssh_connections(
        active_concurrency_group,
        ["sh", str(script_path)],
        {"MNGR_SSH_TRANSPORT_PROBE_VALUE": probe_value},
        deadline_seconds=5.0,
        backoff_seconds=0.0,
    )

    assert finished.stdout == probe_value
