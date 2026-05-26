"""Auto-register newly-discovered agents in their host's latchkey permissions file.

Wires a callback onto :class:`MngrCliBackendResolver` that watches the
discovery stream for new ``(host_id, agent_id)`` pairs and, for each one
on a minds-managed host (i.e. a host whose ``latchkey_permissions.json``
already exists), appends the agent to the ``minds-api-proxy-per-agent-unauthorized``
``not.anyOf`` allowlist so the gateway's ``minds-api-proxy`` extension
stops rejecting the agent's ``/api/v1/agents/<agent_id>/...`` calls.

This is the only place that drives latchkey registration for *every* way
an agent can come into existence:

- The top-level ``mngr create`` invoked by :class:`AgentCreator` (the
  ``/api/create-agent`` flow and the create-project form).
- Sibling creations spawned inside a workspace by the system_interface
  app's "new chat" / "new worktree" buttons -- these shell out to
  ``mngr create`` on the workspace host with no callback into minds,
  so the only signal minds gets is the local ``mngr observe`` discovery
  stream picking the new agent up.
- Any other path that lands an agent on a minds-managed host (manual
  CLI use, scripts, ``mngr_uncapped_claude`` orchestration, etc.).

Hosts without an existing permissions file are intentionally skipped:
the file is materialized at host-creation time by
:func:`imbue.mngr_latchkey.agent_setup.finalize_host_permissions`, so
its absence means the host was not provisioned by minds and we should
not conjure a permissions file from a discovery event alone.
"""

import threading

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host


class LatchkeyAutoRegister(MutableModel):
    """Subscribes to ``MngrCliBackendResolver`` and registers newly-seen agents.

    Holds an in-memory set of ``(host_id, agent_id)`` pairs already
    processed so the on-change callback can short-circuit on the common
    "discovery tick with no new agents" case without re-reading the
    permissions file. The underlying :func:`register_agent_for_host`
    is itself idempotent + atomic, so the dedup set is purely an
    optimization -- correctness does not depend on it.

    Process-scoped lifetime. Not unsubscribed at shutdown because the
    resolver itself dies with the process; if a finer-grained teardown
    is ever needed, ``MngrCliBackendResolver.remove_on_change_callback``
    can be invoked with :meth:`_on_change`.
    """

    backend_resolver: MngrCliBackendResolver = Field(
        frozen=True,
        description="Discovery state to subscribe to. Must already be receiving updates from the envelope consumer.",
    )
    latchkey: Latchkey = Field(
        frozen=True,
        description=(
            "Latchkey instance whose ``plugin_data_dir`` holds the per-host "
            "``latchkey_permissions.json`` files this callback writes to."
        ),
    )

    # ``(host_id, agent_id)`` pairs we have already either registered or
    # decided to skip. Guarded by ``_lock`` because the resolver fires
    # callbacks from its envelope-consumer thread while tests (and
    # potentially the FastAPI request thread) may also inspect state
    # for assertions.
    _processed_pairs: set[tuple[HostId, AgentId]] = PrivateAttr(default_factory=set)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start(self) -> None:
        """Subscribe to the resolver's change stream.

        Fires :meth:`_on_change` once synchronously so any agents already
        in the resolver at startup (e.g. discovered before the lifespan
        finished wiring everything together) get registered without
        waiting for the next discovery tick.
        """
        self.backend_resolver.add_on_change_callback(self._on_change)
        self._on_change()

    def _on_change(self) -> None:
        """Resolver change callback: register any newly-discovered agents.

        Runs synchronously on the resolver's notifying thread (typically
        the envelope-consumer reader thread). The per-pair work is a
        cheap ``Path.is_file()`` + small atomic JSON read/write; in the
        steady state every pair hits the dedup set and we return without
        touching disk.
        """
        for host_id, agent_id in self._collect_unprocessed_pairs():
            self._handle_pair(host_id, agent_id)

    def _collect_unprocessed_pairs(self) -> list[tuple[HostId, AgentId]]:
        """Return discovered ``(host_id, agent_id)`` pairs not yet processed.

        Snapshotting under the lock keeps the per-pair work outside the
        critical section -- the actual file IO in :meth:`_handle_pair`
        runs without the lock held.
        """
        discovered = self.backend_resolver.list_discovered_agents()
        with self._lock:
            return [
                (agent.host_id, agent.agent_id)
                for agent in discovered
                if (agent.host_id, agent.agent_id) not in self._processed_pairs
            ]

    def _handle_pair(self, host_id: HostId, agent_id: AgentId) -> None:
        """Register ``agent_id`` on ``host_id`` if the host is minds-managed.

        Hosts without an existing ``latchkey_permissions.json`` are
        skipped (see module docstring). On infrastructure failure
        (malformed file, IO error) we log a warning and still mark the
        pair as processed so we do not retry on every subsequent
        discovery tick -- the operator can recover with
        ``mngr latchkey register-agent`` once the underlying file
        issue is resolved.
        """
        permissions_path = permissions_path_for_host(self.latchkey.plugin_data_dir, host_id)
        if not permissions_path.is_file():
            logger.debug(
                "Skipping latchkey auto-register for agent {} on host {}: no permissions file at {}",
                agent_id,
                host_id,
                permissions_path,
            )
            with self._lock:
                self._processed_pairs.add((host_id, agent_id))
            return

        try:
            register_agent_for_host(self.latchkey.plugin_data_dir, host_id, agent_id)
        except LatchkeyStoreError as e:
            logger.warning(
                "Failed to auto-register agent {} on host {} in latchkey permissions: {}",
                agent_id,
                host_id,
                e,
            )
        else:
            logger.debug(
                "Auto-registered agent {} on host {} in latchkey permissions",
                agent_id,
                host_id,
            )
        with self._lock:
            self._processed_pairs.add((host_id, agent_id))
