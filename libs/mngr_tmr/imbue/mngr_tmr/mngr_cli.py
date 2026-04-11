"""Subprocess wrapper for mngr CLI commands.

Instead of importing and calling mngr's internal Python API, this module
shells out to the ``mngr`` CLI binary.  This decouples the TMR plugin from
mngr internals and makes the interaction boundary explicit.
"""

import json

from loguru import logger
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.api.list import ListResult
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails


class CliError(MngrError):
    """Raised when a mngr CLI invocation fails (non-zero exit, unparseable output, etc.)."""

    ...


_LIST_AGENTS_TIMEOUT_SECONDS = 60.0


def _run_mngr_raw(
    args: list[str],
    cg: ConcurrencyGroup,
    timeout: float,
) -> FinishedProcess:
    """Run a mngr CLI command and return the raw result.

    Raises CliError when the process times out.
    """
    cmd = ["mngr", *args]
    logger.debug("Running CLI: {}", " ".join(cmd))
    result = cg.run_process_to_completion(cmd, timeout=timeout, is_checked_after=False)
    if result.is_timed_out:
        raise CliError("mngr {} timed out after {:.0f}s".format(args[0] if args else "unknown", timeout))
    return result


def _run_mngr(
    args: list[str],
    cg: ConcurrencyGroup,
    timeout: float,
) -> str:
    """Run a mngr CLI command and return its stdout.

    Raises CliError when the command times out or exits with a non-zero return code.
    """
    result = _run_mngr_raw(args, cg, timeout)
    if result.returncode != 0:
        raise CliError(
            "mngr {} failed (exit code {}): {}".format(
                args[0] if args else "unknown",
                result.returncode,
                result.stderr.strip(),
            )
        )
    return result.stdout


def _parse_list_json(raw_json: str) -> ListResult:
    """Parse the JSON output of ``mngr list --format json`` into a ListResult.

    Raises CliError if the JSON cannot be parsed.
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CliError("mngr list produced invalid JSON: {}".format(str(exc))) from exc
    try:
        agents = [AgentDetails.model_validate(agent_data) for agent_data in data.get("agents", [])]
    except ValidationError as exc:
        raise CliError("mngr list produced JSON with unexpected schema: {}".format(str(exc))) from exc
    return ListResult(agents=agents)


def list_agents(
    cg: ConcurrencyGroup,
    timeout: float = _LIST_AGENTS_TIMEOUT_SECONDS,
) -> ListResult:
    """List agents by calling ``mngr list --format json``.

    ``mngr list`` may exit with code 1 when individual providers fail
    while still returning valid JSON with agents from healthy providers.
    We parse the JSON output regardless of the exit code and only raise
    when the output cannot be parsed at all.
    """
    result = _run_mngr_raw(["list", "--format", "json"], cg, timeout)
    if result.stdout.strip():
        return _parse_list_json(result.stdout)
    if result.returncode != 0:
        raise CliError(
            "mngr list failed (exit code {}): {}".format(
                result.returncode,
                result.stderr.strip(),
            )
        )
    return ListResult()
