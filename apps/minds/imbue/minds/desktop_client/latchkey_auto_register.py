"""Auto-register newly-discovered agents in their host's latchkey permissions file.

Wires a callback onto :class:`MngrCliBackendResolver` that watches the
discovery stream for new ``(host_id, agent_id)`` pairs and, for each one
on a minds-managed host (i.e. a host whose ``latchkey_permissions.json``
exists), appends the agent to the ``minds-api-proxy-per-agent-unauthorized``
``not.anyOf`` allowlist so the gateway's ``minds-api-proxy`` extension
stops rejecting the agent's ``/api/v1/agents/<agent_id>/...`` calls.

A pair whose host file is not there yet is retried on later resolver changes
rather than dropped: on a brand-new workspace, discovery beats the file into
existence.

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

    The underlying :func:`register_agent_for_host` is itself idempotent, so the
    dedup set is purely an optimization -- correctness does not depend on it. It
    is *not* atomic, though (it reads the host file, appends, and writes back),
    which is why :meth:`_handle_pair` serializes it under ``_lock``.

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

    # ``(host_id, agent_id)`` pairs we have reached a terminal verdict on --
    # registered, or failed against a permissions file we cannot repair.
    # Guarded by ``_lock`` because the resolver fires callbacks from its
    # envelope-consumer thread while tests (and potentially the FastAPI request
    # thread) may also inspect state for assertions.
    _processed_pairs: set[tuple[HostId, AgentId]] = PrivateAttr(default_factory=set)

    # Pairs *still* waiting on a host permissions file. Deliberately kept out of
    # ``_processed_pairs`` so a later resolver change retries them; this set
    # exists only to keep the deferral off the log after the first time, so a
    # pair is dropped again once it reaches a terminal verdict.
    _deferred_pairs: set[tuple[HostId, AgentId]] = PrivateAttr(default_factory=set)
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
        """Resolver change callback: register any newly-discovered agents."""
        for host_id, agent_id in self._collect_unprocessed_pairs():
            self._handle_pair(host_id, agent_id)

    def _collect_unprocessed_pairs(self) -> list[tuple[HostId, AgentId]]:
        """Return discovered ``(host_id, agent_id)`` pairs not yet processed.

        Snapshotted rather than iterated under the lock because
        :meth:`_handle_pair` takes the same (non-reentrant) lock for the
        duration of each pair's file work.
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

        Hosts without an existing ``latchkey_permissions.json`` are *deferred*,
        not dropped. A brand-new workspace reaches discovery before agent
        creation's ``finalize_host_permissions`` links the file into place, so
        treating the absence as final would leave the workspace's own agent out
        of the host's ``minds-api-proxy`` allowlist for the rest of the app's
        lifetime -- every ``/api/v1/agents/<id>/...`` call from it rejected 403.
        Retrying settles as soon as the file appears. Its cost is one ``stat``
        per *resolver change* -- which is more often than per discovery tick:
        besides the observe stream's agent and provider snapshots, ``_on_change``
        fires on every service event a workspace emits and on the many request
        paths that call ``notify_change``. A host that genuinely is not
        minds-managed pays that stat forever, since we still refuse to conjure it
        a file from a discovery event alone. Hence the one-shot deferral log: the
        stat is cheap, a log line per resolver change is not.

        On infrastructure failure (malformed file, IO error) we log a warning and
        mark the pair as processed so we do not retry on every subsequent
        discovery tick -- the operator can recover with ``mngr latchkey
        register-agent`` once the underlying file issue is resolved.

        ``_lock`` is held for the whole body, including the file work.
        :func:`register_agent_for_host` reads the host file, appends this agent
        to the allowlist and writes it back, and change callbacks fire from
        several threads at once (the envelope consumer plus any request thread
        calling ``notify_change``). Two threads registering different agents on
        one host would otherwise interleave that read-modify-write and drop one
        of them -- permanently, since the loser is marked processed all the same.
        """
        permissions_path = permissions_path_for_host(self.latchkey.plugin_data_dir, host_id)
        with self._lock:
            # Two threads can collect the same pair before either finishes it.
            if (host_id, agent_id) in self._processed_pairs:
                return
            if not permissions_path.is_file():
                is_first_deferral = (host_id, agent_id) not in self._deferred_pairs
                self._deferred_pairs.add((host_id, agent_id))
                if is_first_deferral:
                    logger.debug(
                        "Deferring latchkey auto-register for agent {} on host {}: no permissions file at {} yet",
                        agent_id,
                        host_id,
                        permissions_path,
                    )
                return

            try:
                register_agent_for_host(self.latchkey.plugin_data_dir, host_id, agent_id)
            except (LatchkeyStoreError, OSError) as e:
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
            self._processed_pairs.add((host_id, agent_id))
            self._deferred_pairs.discard((host_id, agent_id))
