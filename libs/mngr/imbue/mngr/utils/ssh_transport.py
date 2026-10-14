"""The ssh transport that rsync and git borrow: recognizing when its connection was closed, and riding that out.

Sits beside ``utils.ssh``, which renders ssh invocations as text and reads nothing
back. This module reads what the transport printed when it failed, and re-runs the
command that was using it.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from loguru import logger
from tenacity import RetryCallState
from tenacity import Retrying
from tenacity import retry_if_exception
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.pure import pure

# What OpenSSH and rsync print when the peer closed the connection out from under the
# command, rather than the command itself failing. All three say the same thing in
# different voices:
#   - "kex_exchange_identification": OpenSSH names the protocol-version exchange it was
#     still in when the peer closed or reset the connection (the shape git push reports).
#   - "Connection closed by <address> port <port>": the peer hung up and OpenSSH had no
#     protocol stage to name for it (the shape rsync's ssh reports).
#   - rsync's own byte count, which states outright that nothing had arrived.
# A Modal sandbox tunnel produces all three *seconds after* a readiness probe has already
# opened an authenticated session through it, while connections opened before the window
# keep working straight through it -- so an endpoint that answered a moment ago is no
# evidence about the next connection to it. Refused, unreachable, auth and host-key
# failures are deliberately absent: they carry other messages, so a host that is
# genuinely down or mispinned still fails on the first attempt.
_CLOSED_SSH_CONNECTION_STDERR_MESSAGES: Final[tuple[str, ...]] = (
    "kex_exchange_identification",
    "Connection closed by ",
    "connection unexpectedly closed (0 bytes received so far)",
)


@pure
def is_closed_ssh_connection_stderr(stderr: str) -> bool:
    """Whether this stderr says the peer closed the ssh connection, rather than the command failing."""
    return any(message in stderr for message in _CLOSED_SSH_CONNECTION_STDERR_MESSAGES)


# How long a caller keeps re-establishing an ssh connection that a freshly provisioned or
# tunnel-fronted sshd is not completing yet, and how long it pauses between tries. One
# budget for every transport that hits that window, since the window is the host's and not
# the transport's. A failure in it surfaces either immediately or only after a full banner
# timeout, so a fixed count of zero-wait retries can burn every attempt in milliseconds
# before sshd is ready -- ride the race out over a wall-clock deadline instead, so the
# window does not depend on how each individual attempt happens to fail.
SSH_CONNECT_HANDSHAKE_RETRY_DEADLINE_SECONDS: Final[float] = 30.0
SSH_CONNECT_HANDSHAKE_RETRY_BACKOFF_SECONDS: Final[float] = 0.5


def _is_closed_ssh_connection_process_error(exception: BaseException) -> bool:
    return isinstance(exception, ProcessError) and is_closed_ssh_connection_stderr(exception.stderr)


def _log_closed_ssh_connection_retry(retry_state: RetryCallState) -> None:
    logger.debug(
        "Retrying a command whose ssh connection the peer closed (attempt {})",
        retry_state.attempt_number,
    )


def run_process_retrying_closed_ssh_connections(
    concurrency_group: ConcurrencyGroup,
    command: Sequence[str],
    env: Mapping[str, str] | None,
    deadline_seconds: float,
    backoff_seconds: float,
) -> FinishedProcess:
    """Run a command that reaches its target over ssh, re-running it while the peer keeps closing the connection.

    Only for commands that are safe to run again from the start: an rsync, or a git
    fetch/push whose refspecs are explicit and forced. A closed connection leaves the
    remote side with no command to have half-finished, and re-running one of these
    reaches the same end state either way.

    Raises the last ``ProcessError`` once the deadline passes, so callers keep the same
    failure they got before any of this.
    """
    retrying = Retrying(
        retry=retry_if_exception(_is_closed_ssh_connection_process_error),
        stop=stop_after_delay(deadline_seconds),
        wait=wait_fixed(backoff_seconds),
        reraise=True,
        before_sleep=_log_closed_ssh_connection_retry,
    )
    return retrying(lambda: concurrency_group.run_process_to_completion(list(command), env=env))
