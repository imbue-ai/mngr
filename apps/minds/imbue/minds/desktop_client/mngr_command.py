"""Run ``mngr`` subprocesses to completion for the lower-level desktop-client modules.

Extracted so the agent-facing ``/api/v1`` handlers and their lower modules
(``workspace_settings``, ``desktop_control``) can shell out to ``mngr`` with the
same raise-on-failure policy without importing ``app.py`` (which would be an
import cycle). A non-clean outcome surfaces as the single ``MngrCommandError``
callers already catch (a timeout as the more specific
``MngrCommandTimeoutError``), carrying mngr's verdict as its message and a
bounded tail of what the subprocess printed. ``workspace_recovery``'s
``_run_mngr`` / ``_run_mngr_capturing`` pair, which needs the returncode rather
than an exception, applies that same policy from the helpers here.
"""

from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError

# Generous ceiling sized for a host label / stop-host write.
MNGR_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0

# Cap on each captured stream's tail carried by MngrCommandError, and on the
# verdict extracted from stderr. Sized to keep the last few dozen log lines (the
# step the command died in) without ever making the error object itself heavyweight.
OUTPUT_TAIL_MAX_CHARS: Final[int] = 4000

# Prefixes that mark the start of mngr's fatal verdict on stderr. ``Error:`` is
# what ``ClickException.show`` writes straight to the stream once the command has
# returned, so it lands last and appears at every verbosity (including
# ``--quiet``, which silences loguru entirely); ``ERROR:`` is loguru's own
# ``logger.error`` prefix. Neither carries ANSI escapes here -- mngr colors only
# when its stderr is a terminal, and this one is a capture pipe.
_MNGR_VERDICT_PREFIXES: Final[tuple[str, ...]] = ("Error:", "ERROR:")


def format_output_tail(stdout: str, stderr: str) -> str | None:
    """Bounded tails of a subprocess's captured output, or None when it wrote nothing."""
    parts = []
    for name, text in (("stdout", stdout), ("stderr", stderr)):
        stripped = text.strip()
        if stripped:
            parts.append(f"--- {name} tail ---\n{stripped[-OUTPUT_TAIL_MAX_CHARS:]}")
    if not parts:
        return None
    return "\n".join(parts)


def mngr_failure_verdict(stderr: str) -> str:
    """Just the fatal-error block from a failed ``mngr`` run's stderr, bounded.

    An mngr run at DEBUG verbosity (which the recovery argv asks for, so that the
    tail carries a step timeline) puts the whole timeline on stderr with the
    verdict at the end. Only the verdict may reach ``str(exc)``: that text is
    rendered to the user as a failure message, and it is what the substring
    consumers key on -- the shutdown-not-supported match and
    ``_in_band_provider_outage_reason``. Both of those would otherwise read
    mngr's *tolerated* skips: a provider that mngr skipped as unavailable and
    then continued past is logged at DEBUG with the verbatim
    ``ProviderUnavailableError`` message the outage parser matches, so a command
    that died of something unrelated would be reported as this machine's backend
    going down. The full output is still kept, on ``MngrCommandError.output_tail``.

    Everything from the marker onward is kept rather than the marker's line
    alone, because a verdict spans lines: mngr appends its bracketed help text,
    and a provider's own reason can itself be multi-line.

    A run that produced no verdict at all (an unhandled traceback) falls back to
    the bounded stderr tail, which is then the only diagnosis there is.
    """
    lines = stderr.strip().splitlines()
    for index in reversed(range(len(lines))):
        if lines[index].startswith(_MNGR_VERDICT_PREFIXES):
            return "\n".join(lines[index:])[:OUTPUT_TAIL_MAX_CHARS]
    return stderr.strip()[-OUTPUT_TAIL_MAX_CHARS:]


def run_mngr_to_completion(
    concurrency_group: ConcurrencyGroup,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float = MNGR_COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Run an ``mngr`` subprocess to completion and return its stdout on a clean exit.

    Raises ``MngrCommandError`` for every non-clean outcome (launch failure,
    nonzero exit), or ``MngrCommandTimeoutError`` (a ``MngrCommandError``
    subclass) on a timeout, so callers catch the one domain error. The message
    carries mngr's verdict alone; whatever the subprocess printed rides
    ``MngrCommandError.output_tail``.
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
        # A killed subprocess never printed a verdict, so its captured output is
        # the only record of which step it died in; carry it on the error
        # (bounded, out of the message) instead of discarding it.
        raise MngrCommandTimeoutError(
            f"timed out after {int(timeout_seconds)}s",
            output_tail=format_output_tail(finished.stdout, finished.stderr),
        )
    returncode = finished.returncode if finished.returncode is not None else 1
    if returncode != 0:
        # Only mngr's verdict rides the message; the timeline it printed getting
        # there rides the tail, exactly as the timeout path above does.
        raise MngrCommandError(
            f"exited {returncode}: {mngr_failure_verdict(finished.stderr)}",
            output_tail=format_output_tail(finished.stdout, finished.stderr),
        )
    return finished.stdout
