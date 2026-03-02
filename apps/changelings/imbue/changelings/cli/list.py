import json
import sys
from typing import Any

import click
from loguru import logger
from tabulate import tabulate

from imbue.changelings.config.data_types import MNG_BINARY
from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

_DEFAULT_DISPLAY_FIELDS = (
    "name",
    "id",
    "state",
    "host.provider_name",
    "host.state",
)

_HEADER_LABELS: dict[str, str] = {
    "name": "NAME",
    "id": "ID",
    "state": "STATE",
    "host.state": "HOST STATE",
    "host.name": "HOST",
    "host.provider_name": "PROVIDER",
}


def _fetch_changeling_agents_json() -> list[dict[str, Any]]:
    """Call `mng list --label changeling=true --json --quiet` and return the agents list.

    Filters to only agents with the changeling=true label (set during
    changeling deploy). Returns an empty list on failure.
    """
    cg = ConcurrencyGroup(name="changeling-list")
    try:
        with cg:
            result = cg.run_process_to_completion(
                command=[MNG_BINARY, "list", "--label", "changeling=true", "--json", "--quiet"],
                is_checked_after=False,
            )
    except ConcurrencyExceptionGroup as e:
        logger.warning("Failed to run mng list: {}", e)
        return []

    if result.returncode != 0:
        error_detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        logger.warning("mng list failed (exit code {}): {}", result.returncode, error_detail)
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse mng list output: {}", e)
        return []

    return data.get("agents", [])


def _get_field_value(agent: dict[str, Any], field: str) -> str:
    """Extract a field value from a mng agent dict, supporting dotted paths."""
    parts = field.split(".")
    value: Any = agent
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    if value is None:
        return ""
    return str(value)


def _build_table(
    agents: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> list[list[str]]:
    """Build table rows from mng agent data."""
    rows: list[list[str]] = []
    for agent in agents:
        row = [_get_field_value(agent, field) for field in fields]
        rows.append(row)
    return rows


def _emit_human_output(
    agents: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> None:
    """Print a human-readable table of changelings."""
    if not agents:
        logger.info("No changelings found")
        return

    headers = [_HEADER_LABELS.get(f, f.upper()) for f in fields]
    rows = _build_table(agents, fields)
    table = tabulate(rows, headers=headers, tablefmt="plain")
    logger.info("{}", table)


def _emit_json_output(agents: list[dict[str, Any]]) -> None:
    """Print JSON output with changeling info."""
    sys.stdout.write(json.dumps({"changelings": agents}, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


@click.command(name="list")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output in JSON format",
)
def list_command(
    output_json: bool,
) -> None:
    """List deployed changelings.

    Queries mng for agents with the changeling=true label to show
    the current state of each deployed changeling.

    Example:

    \b
        changeling list
        changeling list --json
    """
    agents = _fetch_changeling_agents_json()

    if output_json:
        _emit_json_output(agents)
    else:
        _emit_human_output(agents, _DEFAULT_DISPLAY_FIELDS)
