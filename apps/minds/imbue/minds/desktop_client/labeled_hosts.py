"""CLI-backed host inventory for the workspace-id-labeled Lima / Docker hosts.

Minds stamps every Lima / Docker workspace host with a ``workspace-id`` label
(the opaque pending-create-attempt id) at create time. Both the startup reconcile
and the create attempt discard / retry flows need to walk that inventory -- listing
one provider's hosts (agent-less ones included) through ``mngr list --hosts``
and joining them back to pending-create-attempt records through the label. This
module is the shared lower layer for that: ``startup_reconcile`` and
``agent_creator`` both import it, so neither has to import the other.
"""

import json
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.mngr_command import run_mngr_to_completion
from imbue.minds.desktop_client.pending_create_attempts import WORKSPACE_ID_HOST_LABEL

# The provider instances whose hosts carry the ``workspace-id`` label (the
# create stamps it only for the local-VM modes): the scope of both the startup
# reconcile and the create attempt discard flows. Modal is excluded (sandboxes
# self-expire within ~a day), imbue_cloud pool hosts have their own reconcile,
# and the cloud BYOK providers were never part of the orphan failure mode.
WORKSPACE_ID_LABELED_PROVIDER_NAMES: Final[tuple[str, ...]] = ("lima", "docker")


class ListedHostAgent(FrozenModel):
    """One agent row from ``mngr list --hosts --format json``."""

    id: str = Field(description="Agent id")
    name: str = Field(description="Agent name")


class ListedHost(FrozenModel):
    """One host row from ``mngr list --hosts --format json``."""

    id: str = Field(description="Host id")
    name: str = Field(description="Host name (slug)")
    provider: str = Field(description="Provider instance name")
    state: str | None = Field(default=None, description="Host lifecycle state, when known")
    labels: dict[str, str] = Field(default_factory=dict, description="Host labels (user tags)")
    agents: tuple[ListedHostAgent, ...] = Field(default=(), description="Agents discovered on the host")


def parse_hosts_listing(json_text: str) -> list[ListedHost]:
    """Parse ``mngr list --hosts --format json`` output into typed host rows.

    A malformed document returns an empty list with a warning (the caller
    then simply does nothing with this inventory) rather than crashing the
    app over subprocess output.
    """
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.warning("Could not parse the mngr hosts listing: {}", e)
        return []
    raw_hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(raw_hosts, list):
        logger.warning("Unexpected mngr hosts listing shape: {!r}", type(raw_hosts))
        return []
    hosts: list[ListedHost] = []
    for raw_host in raw_hosts:
        try:
            hosts.append(ListedHost.model_validate(raw_host))
        except ValueError as e:
            logger.warning("Skipping unparseable host row {!r}: {}", raw_host, e)
    return hosts


def list_provider_hosts(
    concurrency_group: ConcurrencyGroup,
    mngr_binary: str,
    env: dict[str, str],
    provider_name: str,
    timeout_seconds: float,
) -> list[ListedHost]:
    """List one provider's hosts (agent-less ones included) through the CLI.

    Raises ``MngrCommandError`` when the listing subprocess fails; callers
    decide whether that degrades to a warning or aborts their flow.
    """
    argv = [mngr_binary, "list", "--hosts", "--provider", provider_name, "--format", "json"]
    stdout = run_mngr_to_completion(concurrency_group, argv, env, timeout_seconds=timeout_seconds)
    return parse_hosts_listing(stdout)


def find_host_by_workspace_id_label(hosts: list[ListedHost], workspace_id: str) -> ListedHost | None:
    """Find the host carrying ``workspace_id`` as its ``workspace-id`` label, if any.

    Terminal (FAILED / DESTROYED) host records are skipped: they are dead
    state a gc sweep owns, not a live host a discard needs to tear down.
    """
    for host in hosts:
        if host.state in ("FAILED", "DESTROYED"):
            continue
        if host.labels.get(WORKSPACE_ID_HOST_LABEL) == workspace_id:
            return host
    return None
