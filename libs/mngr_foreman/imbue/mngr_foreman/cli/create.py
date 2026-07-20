"""``mngr foreman create`` -- create an agent tagged for foreman.

A thin passthrough to ``mngr create`` that adds ``--label foreman=1`` so
``mngr foreman serve --foreman-only`` can filter to foreman-created agents. Every
other flag/argument is forwarded verbatim.

With ``--bootstrap <script>`` the create is run with ``--format json`` so the new
agent's id can be parsed, then the script's contents are run on the agent's host
(in its work_dir) via ``mngr exec`` -- which calls ``exec_command_on_agent``
underneath. This runs the whole thing as subprocess orchestration, so it inherits
the user's exact ``mngr`` config/providers with no in-process context to keep in
sync.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.mngr_foreman.mngr_bin import resolve_mngr_binary

_FOREMAN_LABEL: Final[str] = "foreman=1"


def _parse_created_agent_id(stdout: str) -> str | None:
    """Extract ``agent_id`` from ``mngr create --format json`` output.

    The JSON object is emitted as a line among possibly-noisier output, so scan
    lines from the end for the first that parses to a dict carrying ``agent_id``.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("agent_id"):
            return str(data["agent_id"])
    return None


@click.command(
    name="create",
    context_settings={"ignore_unknown_options": True},
)
@click.option(
    "--bootstrap",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a shell script to run on the new agent's host (in its work_dir) after create.",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def create(ctx: click.Context, bootstrap: Path | None, args: tuple[str, ...]) -> None:
    """Create an agent labelled foreman=1 (forwards all args to `mngr create`).

    \b
    Examples:
      mngr foreman create my-agent --new-host --in modal
      mngr foreman create my-agent --bootstrap ./setup.sh
    """
    mngr_binary = resolve_mngr_binary()
    base_command = [mngr_binary, "create", *args, "--label", _FOREMAN_LABEL]

    if bootstrap is None:
        # Pure passthrough: inherit stdio so interactive create (prompts,
        # --connect) works, and propagate the exit code.
        result = subprocess.run(base_command, check=False)  # noqa: S603 - args are the user's own
        ctx.exit(result.returncode)

    # Bootstrap path: capture JSON so we can find the created agent, then exec.
    script = bootstrap.read_text()
    create_command = [*base_command, "--format", "json"]
    logger.info("Creating foreman agent, then bootstrapping from {}", bootstrap)
    proc = subprocess.run(  # noqa: S603 - args are the user's own
        create_command, check=False, capture_output=True, text=True
    )
    # Surface create output verbatim regardless of outcome.
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise click.ClickException(f"`mngr create` failed (exit {proc.returncode}); skipping bootstrap.")

    agent_id = _parse_created_agent_id(proc.stdout)
    if agent_id is None:
        raise click.ClickException("Could not determine the created agent id from `mngr create` output; skipping bootstrap.")

    click.echo(f"Bootstrapping agent {agent_id}...")
    exec_command = [mngr_binary, "exec", agent_id, script]
    exec_result = subprocess.run(exec_command, check=False)  # noqa: S603 - agent_id is machine-derived, script is the user's
    if exec_result.returncode != 0:
        raise click.ClickException(f"Bootstrap script failed (exit {exec_result.returncode}).")
