"""Run ``mngr`` subprocesses to completion for the lower-level desktop-client modules.

Extracted so the agent-facing ``/api/v1`` handlers and their lower modules
(``workspace_settings``, ``desktop_control``) can shell out to ``mngr`` with the
same raise-on-failure policy without importing ``app.py`` (which would be an
import cycle). This mirrors ``app.py``'s own ``_run_mngr`` / ``_run_mngr_capturing``
pair: a non-clean outcome surfaces as the single ``MngrCommandError`` callers
already catch (a timeout as the more specific ``MngrCommandTimeoutError``).
"""

from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError

# Generous ceiling sized for a host label / stop-host write.
MNGR_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0


def run_mngr_to_completion(
    concurrency_group: ConcurrencyGroup,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float = MNGR_COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Run an ``mngr`` subprocess to completion and return its stdout on a clean exit.

    Raises ``MngrCommandError`` for every non-clean outcome (launch failure,
    nonzero exit), or ``MngrCommandTimeoutError`` (a ``MngrCommandError``
    subclass) on a timeout, so callers catch the one domain error.
    """
    cg = concurrency_group.make_concurrency_group(name="mngr-command")
    try:
        with cg:
            finished = cg.run_process_to_completion(
                argv,
                timeout=timeout_seconds,
                is_checked_after=False,
                env=env,
            )
    except (OSError, ConcurrencyGroupError) as exc:
        raise MngrCommandError(str(exc)) from exc
    if finished.is_timed_out:
        raise MngrCommandTimeoutError(f"timed out after {int(timeout_seconds)}s")
    returncode = finished.returncode if finished.returncode is not None else 1
    if returncode != 0:
        raise MngrCommandError(f"exited {returncode}: {finished.stderr.strip()}")
    return finished.stdout
