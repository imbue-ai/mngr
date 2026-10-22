"""Auto-register newly-discovered agents in their host's latchkey permissions file.

Wires a callback onto :class:`MngrCliBackendResolver` that watches the
discovery stream for new ``(host_id, agent_id)`` pairs and, for each one
on a host Imbue Studio manages (i.e. a host whose ``latchkey_permissions.json``
exists), appends the agent to the ``minds-api-proxy-per-agent-unauthorized``
``not.anyOf`` allowlist so the gateway's ``minds-api-proxy`` extension
stops rejecting the agent's ``/api/v1/agents/<agent_id>/...`` calls.

A pair whose host file is not there yet is retried on later resolver changes
rather than dropped: on a brand-new workspace, discovery beats the file into
existence.

A registration that changes the host file is also pushed to the host's own
machine, when it has one: a remote workspace's gateway enforces its own copy of
the policy, and the baseline ``register_agent_for_host`` brings up to date is
checked there. The push runs off the resolver's callback thread (which must
stay non-blocking) and is coalesced per host, so a burst of registrations costs
one round trip that carries the latest snapshot.

A push for a host whose machine is not provisioned yet is held, not dropped,
and retried on later resolver changes once it is -- the same deferral the
registration itself gets. On a brand-new remote workspace the registration
beats the provisioning pass that gives the machine its key: the host file
lands when ``mngr create`` returns, the key seconds later, and a push made in
between finds no machine to carry to. Dropping it would leave the gateway on
the deny-all default it was seeded with, refusing the workspace's own agent
until something else pushed the policy.
"""

import threading
from collections.abc import Callable

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.remote.credentials import has_machine_of_its_own
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host


class LatchkeyAutoRegister(MutableModel):
    """Subscribes to ``MngrCliBackendResolver`` and registers newly-seen agents.

    The underlying :func:`register_agent_for_host` is itself idempotent, so the
    dedup set is purely an optimization -- correctness does not depend on it. It
    is *not* atomic, though (it reads the host file, appends, and writes back),
    which is why :meth:`_handle_pair` serializes it under ``_lock``.

    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

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
    push_permissions_to_machine: Callable[[str], None] = Field(
        frozen=True,
        description=(
            "Makes this computer's copy of a workspace's policy the one its machine enforces, given the "
            "workspace's agent id; a no-op for a workspace with no machine of its own. Raises "
            "``MachineOperationError`` when the machine does not take it."
        ),
    )
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description="Owns the background threads the pushes run on; the app's root group, so they live as long as it.",
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

    # Hosts whose file changed since their machine last received it, each with an
    # agent on it (how the push addresses the workspace), and the hosts a push
    # worker is currently running for. Both guarded by ``_push_lock``: the worker
    # keeps going while its host is re-marked, so a change made mid-push is
    # carried by the next iteration rather than by a second, racing thread.
    _hosts_awaiting_push: dict[HostId, AgentId] = PrivateAttr(default_factory=dict)
    _hosts_being_pushed: set[HostId] = PrivateAttr(default_factory=set)
    # Hosts whose changed file found no machine to go to yet, each with the
    # agent the push is addressed by; re-scheduled on a later resolver change
    # once the machine has been provisioned. Guarded by ``_push_lock``.
    _hosts_awaiting_machine: dict[HostId, AgentId] = PrivateAttr(default_factory=dict)
    _push_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

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
        """Resolver change callback: register any newly-discovered agents, and retry the pushes held for a machine."""
        for host_id, agent_id in self._collect_unprocessed_pairs():
            self._handle_pair(host_id, agent_id)
        for host_id, agent_id in self._collect_hosts_whose_machine_appeared():
            self._schedule_push(host_id, agent_id)

    def _collect_hosts_whose_machine_appeared(self) -> list[tuple[HostId, AgentId]]:
        """The held pushes whose host now has a machine of its own.

        One ``stat`` of the host's machine key per held host per resolver
        change, like the deferred registrations' ``stat`` of the host file --
        and, like those, paid for as long as the host has no machine: a local
        host never gets one, so its held push is never carried, which is the
        right outcome for a host whose gateway reads the file here.
        """
        with self._push_lock:
            held = list(self._hosts_awaiting_machine.items())
        return [(host_id, agent_id) for host_id, agent_id in held if self._has_machine(host_id)]

    def _has_machine(self, host_id: HostId) -> bool:
        """Whether ``host_id`` has a machine a push could go to (see ``has_machine_of_its_own``).

        A key record that cannot be read counts as a machine: the push then
        fails against the same record and reports it, rather than being held
        in silence for a machine that may well be there.
        """
        try:
            return has_machine_of_its_own(self.latchkey.plugin_data_dir, host_id)
        except LatchkeyStoreError as e:
            logger.debug(
                "Could not read the machine key record of host {}; treating it as a machine so the push reports it: {}",
                host_id,
                e,
            )
            return True

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
        """Register ``agent_id`` on ``host_id`` if Imbue Studio manages the host.

        Hosts without an existing ``latchkey_permissions.json`` are *deferred*,
        not dropped. A brand-new workspace reaches discovery before agent
        creation's ``finalize_host_permissions`` links the file into place, so
        treating the absence as final would leave the workspace's own agent out
        of the host's ``minds-api-proxy`` allowlist for the rest of the app's
        lifetime -- every ``/api/v1/agents/<id>/...`` call from it rejected 403.
        Retrying settles at the first resolver change after the file appears;
        agent creation fires one right after linking it. Its cost is one
        ``stat`` per *resolver change*: besides every provider snapshot and
        every changed agents snapshot, ``_on_change`` fires on every service
        change a workspace emits and on the many request paths that call
        ``notify_change``. A host that genuinely is not managed by Imbue
        Studio pays that stat forever, since we still refuse to conjure it
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
                is_file_changed = register_agent_for_host(self.latchkey.plugin_data_dir, host_id, agent_id)
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
                if is_file_changed:
                    self._schedule_push(host_id, agent_id)
            self._processed_pairs.add((host_id, agent_id))
            self._deferred_pairs.discard((host_id, agent_id))

    def _schedule_push(self, host_id: HostId, agent_id: AgentId) -> None:
        """Mark ``host_id``'s file as changed and make sure a worker will carry it over."""
        with self._push_lock:
            self._hosts_awaiting_machine.pop(host_id, None)
            self._hosts_awaiting_push[host_id] = agent_id
            if host_id in self._hosts_being_pushed:
                return
            self._hosts_being_pushed.add(host_id)
        self.concurrency_group.start_new_thread(
            target=self._push_until_settled,
            args=(host_id,),
            name=f"latchkey-auto-register-push-{host_id}",
            is_checked=False,
        )

    def _push_until_settled(self, host_id: HostId) -> None:
        """Push ``host_id``'s policy to its machine until no change is left waiting.

        Each push reads the canonical file at push time, so the last one to run
        carries everything registered up to then.

        A host with no machine yet keeps its change held (see
        :meth:`_collect_hosts_whose_machine_appeared`) rather than pushing
        into nothing: the push would be reported done while the machine, once
        provisioned, enforced the default it was seeded with instead.

        A machine that will not take a push is logged as an error and not
        retried here. There is no user waiting on an auto-registration to tell,
        and the registration does *not* heal itself later: the machine owns the
        policy, so the next read of it adopts the machine's copy over this one
        and the registration is lost. Recover with ``mngr latchkey
        register-agent`` once the machine is reachable again.
        """
        while (agent_id := self._claim_pending_push(host_id)) is not None:
            if not self._has_machine(host_id):
                with self._push_lock:
                    self._hosts_awaiting_machine[host_id] = agent_id
                logger.debug(
                    "Holding the latchkey permissions push for host {} until it has a machine to carry them to",
                    host_id,
                )
                continue
            try:
                self.push_permissions_to_machine(str(agent_id))
            except MachineOperationError as e:
                logger.error(
                    "Failed to push the latchkey permissions of host {} to its machine after registering agent {}; "
                    "the registration will be lost when the machine is next read: {}",
                    host_id,
                    agent_id,
                    e,
                )

    def _claim_pending_push(self, host_id: HostId) -> AgentId | None:
        """Take the change waiting for ``host_id``, releasing the host's worker when there is none."""
        with self._push_lock:
            agent_id = self._hosts_awaiting_push.pop(host_id, None)
            if agent_id is None:
                self._hosts_being_pushed.discard(host_id)
            return agent_id
