"""Run ``mngr`` subprocesses to completion for the lower-level desktop-client modules.

Extracted so the agent-facing ``/api/v1`` handlers and their lower modules
(``workspace_settings``, ``desktop_control``) can shell out to ``mngr`` with the
same raise-on-failure policy without importing ``app.py`` (which would be an
import cycle). ``run_mngr_to_completion`` is that policy: a non-clean outcome
surfaces as the single ``MngrCommandError`` callers already catch (a timeout as
the more specific ``MngrCommandTimeoutError``), carrying mngr's verdict as its
message and a bounded tail of what the subprocess printed.
``run_mngr_capturing`` underneath it serves callers (``workspace_diagnostics``)
that inspect the returncode and output of an unclean exit themselves.
"""

import json
from typing import Final

from loguru import logger

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


def run_mngr_capturing(
    concurrency_group: ConcurrencyGroup,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float = MNGR_COMMAND_TIMEOUT_SECONDS,
) -> tuple[str, int, str]:
    """Run an ``mngr`` subprocess, returning ``(stdout, returncode, stderr)`` without raising on nonzero exit.

    For callers that inspect the output of an unclean exit themselves (a
    sentinel in stdout, a returncode fed into retry logic) rather than wanting
    the one-domain-error policy of :func:`run_mngr_to_completion`. A failure to
    launch the process raises ``MngrCommandError``; a timeout raises the more
    specific ``MngrCommandTimeoutError``.

    The process runs directly on the caller's group -- no child group, whose
    exit would rewrap a launch failure as a ``ConcurrencyExceptionGroup`` that
    is no ``ConcurrencyGroupError`` and would sail past the except below.
    """
    try:
        finished = concurrency_group.run_process_to_completion(
            argv,
            timeout=timeout_seconds,
            is_checked_after=False,
            env=env,
        )
    except (OSError, ConcurrencyGroupError) as exc:
        # The command never ran (a fork/exec failure, or a concurrency-group
        # setup/strand/shutdown failure). Callers handle failure locally, so we
        # wrap it as the single MngrCommandError they already catch.
        raise MngrCommandError(str(exc)) from exc
    if finished.is_timed_out:
        # A killed subprocess never printed a verdict, so its captured output is
        # the only record of which step it died in; carry it on the error
        # (bounded, out of the message) instead of discarding it.
        raise MngrCommandTimeoutError(
            f"timed out after {int(timeout_seconds)}s",
            output_tail=format_output_tail(finished.stdout, finished.stderr),
        )
    # A finished, non-timed-out process always carries a returncode; the Optional
    # is for the not-yet-finished case, which this branch has ruled out.
    returncode = finished.returncode if finished.returncode is not None else 1
    return finished.stdout, returncode, finished.stderr


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
    stdout, returncode, stderr = run_mngr_capturing(concurrency_group, argv, env, timeout_seconds=timeout_seconds)
    if returncode != 0:
        # Only mngr's verdict rides the message; the timeline it printed getting
        # there rides the tail, exactly as the timeout path above does.
        raise MngrCommandError(
            f"exited {returncode}: {mngr_failure_verdict(stderr)}",
            output_tail=format_output_tail(stdout, stderr),
        )
    return stdout


def extract_exec_stdout(exec_json_stdout: str, results_key: str = "results") -> str | None:
    """Unwrap the remote command's own stdout from ``mngr exec --format json`` output.

    ``results_key`` is ``"results"`` for in-container execs and
    ``"outer_results"`` for ``--outer`` ones -- the envelope names them
    differently. Returns None (with a warning logged) when the envelope is
    unparseable, malformed, or reports the remote command as failed; callers
    treat that as "the command never landed", never as content. A well-formed
    envelope whose results list is simply empty is also None, but logged at
    debug only: that is mngr's ordinary answer for an exec it had nowhere to
    run (``--missing-outer ignore`` skips, a failed agent), the envelope's own
    ``skipped_agents``/``failed_agents`` record why, and callers report their
    own failures.
    """
    try:
        envelope = json.loads(exec_json_stdout)
    except json.JSONDecodeError as exc:
        logger.warning("Unparseable mngr exec JSON envelope ({}): {}", exc, exec_json_stdout[:200])
        return None
    results = envelope.get(results_key) if isinstance(envelope, dict) else None
    if isinstance(results, list) and not results:
        logger.debug("mngr exec JSON envelope holds no {} entry: {}", results_key, exec_json_stdout[:200])
        return None
    first = results[0] if isinstance(results, list) and results else None
    if not isinstance(first, dict) or not isinstance(first.get("stdout"), str) or first.get("success") is not True:
        logger.warning("Unexpected mngr exec JSON envelope shape: {}", exec_json_stdout[:200])
        return None
    return first["stdout"]


def extract_exec_failure_detail(exec_json_stdout: str, results_key: str = "results") -> str | None:
    """The failure detail an ``mngr exec --format json`` envelope carries, when there is one.

    In json mode mngr's own stderr says nothing about a failure -- the remote
    command's stderr rides the envelope's result entry, and an exec that never
    reached the agent puts mngr's error in ``failed_agents`` -- so a caller
    quoting a failure in a user-facing message reads it from here. Returns None
    when the envelope is unreadable or holds no failure text.
    """
    try:
        envelope = json.loads(exec_json_stdout)
    except json.JSONDecodeError as exc:
        # Subprocess output, so corruption must be visible: warn rather than
        # swallow, even though ``extract_exec_stdout`` usually warned about the
        # same envelope first.
        logger.warning("Unparseable mngr exec JSON envelope ({}): {}", exc, exec_json_stdout[:200])
        return None
    if not isinstance(envelope, dict):
        return None
    results = envelope.get(results_key)
    first = results[0] if isinstance(results, list) and results else None
    if isinstance(first, dict) and isinstance(first.get("stderr"), str) and first["stderr"].strip():
        return first["stderr"].strip()
    failed_agents = envelope.get("failed_agents")
    first_failed = failed_agents[0] if isinstance(failed_agents, list) and failed_agents else None
    if isinstance(first_failed, dict) and isinstance(first_failed.get("error"), str) and first_failed["error"].strip():
        return first_failed["error"].strip()
    return None
