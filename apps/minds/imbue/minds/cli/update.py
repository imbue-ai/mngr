"""CLI command for updating an existing mind with the latest parent code.

The ``mind update <agent-name>`` command:

1. Stops the mind (via ``mngr stop``)
2. Fetches and merges the latest code from the parent repository
3. Updates all vendored git subtrees
4. Starts the mind back up (via ``mngr start``)
"""

from collections.abc import Mapping
from pathlib import Path

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import parse_agents_from_mngr_output
from imbue.minds.errors import MindError
from imbue.minds.errors import MngrCommandError
from imbue.minds.forwarding_server.agent_creator import load_creation_settings
from imbue.minds.forwarding_server.parent_tracking import fetch_and_merge_parent
from imbue.minds.forwarding_server.parent_tracking import read_parent_info
from imbue.minds.forwarding_server.vendor_mngr import apply_vendor_overrides
from imbue.minds.forwarding_server.vendor_mngr import default_vendor_configs
from imbue.minds.forwarding_server.vendor_mngr import update_vendor_repos
from imbue.mngr.primitives import AgentId


class MindAgentRecord(FrozenModel):
    """Essential fields from a mind agent's ``mngr list`` JSON record.

    Validated on construction so callers get a clear error if required
    fields are missing from the mngr output.
    """

    agent_id: AgentId = Field(description="The agent's unique identifier")
    work_dir: Path = Field(description="Absolute path to the agent's working directory")


def find_mind_agent(agent_name: str) -> MindAgentRecord:
    """Find a mind agent by name using ``mngr list``.

    Searches for agents whose name matches ``agent_name`` (the agent name
    is set to the mind name during creation, so each mind has a unique name).
    Returns a validated MindAgentRecord with the agent's ID and work directory.

    Raises MindError if the agent cannot be found or the record is malformed.
    """
    cg = ConcurrencyGroup(name="mngr-list")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                MNGR_BINARY,
                "list",
                "--include",
                'name == "{}"'.format(agent_name),
                "--format=json",
            ],
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise MindError(
            "Failed to list agents: {}".format(
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip()
            )
        )

    agents = parse_agents_from_mngr_output(result.stdout)
    if not agents:
        raise MindError("No mind found with name '{}'".format(agent_name))

    return parse_mind_agent_record(agents[0], agent_name)


def parse_mind_agent_record(raw: Mapping[str, object], agent_name: str) -> MindAgentRecord:
    """Parse a raw agent dict from ``mngr list`` JSON into a MindAgentRecord.

    Validates that the required ``id`` and ``work_dir`` fields are present.
    Raises MindError if either field is missing.
    """
    raw_id = raw.get("id")
    raw_work_dir = raw.get("work_dir")
    if raw_id is None or raw_work_dir is None:
        raise MindError(
            "Agent record for '{}' is missing required fields (id={}, work_dir={})".format(
                agent_name, raw_id, raw_work_dir
            )
        )

    return MindAgentRecord(agent_id=AgentId(str(raw_id)), work_dir=Path(str(raw_work_dir)))


def _run_mngr_command(verb: str, agent_id: AgentId) -> None:
    """Run an ``mngr <verb> <agent-id>`` command.

    Raises MngrCommandError if the command fails.
    """
    logger.info("Running mngr {} {}...", verb, agent_id)
    cg = ConcurrencyGroup(name="mngr-{}".format(verb))
    with cg:
        result = cg.run_process_to_completion(
            command=[MNGR_BINARY, verb, str(agent_id)],
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise MngrCommandError(
            "mngr {} failed (exit code {}):\n{}".format(
                verb,
                result.returncode,
                result.stderr.strip() if result.stderr.strip() else result.stdout.strip(),
            )
        )


@click.command()
@click.argument("agent_name")
def update(agent_name: str) -> None:
    """Update a mind with the latest code from its parent repository.

    Stops the mind, merges the latest parent code, updates vendored
    subtrees, and starts the mind back up.
    """
    logger.info("Looking up mind '{}'...", agent_name)
    record = find_mind_agent(agent_name)

    logger.info("Found mind '{}' (agent_id={}, work_dir={})", agent_name, record.agent_id, record.work_dir)

    _run_mngr_command("stop", record.agent_id)

    logger.info("Merging latest code from parent repository...")
    parent_info = read_parent_info(record.work_dir)
    new_hash = fetch_and_merge_parent(record.work_dir, parent_info)
    logger.info("Merged parent changes (new hash: {})", str(new_hash)[:12])

    logger.info("Updating vendored subtrees...")
    settings = load_creation_settings(record.work_dir)
    vendor_configs = apply_vendor_overrides(settings.vendor if settings.vendor else default_vendor_configs())
    update_vendor_repos(record.work_dir, vendor_configs)
    logger.info("Vendored subtrees updated ({} configured)", len(vendor_configs))

    _run_mngr_command("start", record.agent_id)

    logger.info("Mind '{}' updated successfully.", agent_name)
