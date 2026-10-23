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

A push for a host whose machine is not provisioned yet is held, not dropped.
On a brand-new remote workspace the registration beats the provisioning pass
that gives the machine its key: the host file lands when ``mngr create``
returns, the key seconds later, and a push made in between finds no machine to
carry to. Dropping it would leave the gateway on the deny-all default it was
seeded with, refusing the workspace's own agent until something else pushed
the policy. The key is recorded by the latchkey plugin's own discovery process,
which tells this app nothing, so a held push watches for the record itself: it
polls the host's machine key for a bounded while, and after that is carried by
the next resolver change that finds the key there (the resolver fires only on a
real change of its state, which a provisioning pass is not).
"""

import threading
import time
from collections.abc import Callable
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.thread_cleanup import start_mngr_thread
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.remote.credentials import has_machine_of_its_own
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host

# How a held push watches for its host's machine key (see the module docstring): the provisioning pass that records
# it finishes seconds after the host file lands, so a few seconds between looks and a few minutes of looking cover
# it, and a host that never gets a key costs one ``stat`` every interval for the watch's duration; after that its
# held push goes back to one ``stat`` per resolver change (``_collect_hosts_whose_machine_appeared``).
DEFAULT_MACHINE_POLL_INTERVAL_SECONDS: Final[float] = 2.0
DEFAULT_MACHINE_WATCH_SECONDS: Final[float] = 600.0


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
    machine_poll_interval_seconds: float = Field(
        default=DEFAULT_MACHINE_POLL_INTERVAL_SECONDS,
        frozen=True,
        description="How often a held push looks for its host's machine key while the watch lasts.",
    )
    machine_watch_seconds: float = Field(
        default=DEFAULT_MACHINE_WATCH_SECONDS,
        frozen=True,
        description=(
            "How long a held push polls for its host's machine key before leaving the retry to resolver changes; "
            "bounded because a local host never gets one."
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

    # Hosts whose file changed since their machine last received it, each with an
    # agent on it (how the push addresses the workspace), and the hosts a push
    # worker is currently running for. Both guarded by ``_push_lock``: the worker
    # keeps going while its host is re-marked, so a change made mid-push is
    # carried by the next iteration rather than by a second, racing thread.
    _hosts_awaiting_push: dict[HostId, AgentId] = PrivateAttr(default_factory=dict)
    _hosts_being_pushed: set[HostId] = PrivateAttr(default_factory=set)
    # Hosts whose changed file found no machine to go to yet, each with the
    # agent the push is addressed by; re-scheduled once the machine has been
    # provisioned, by the host's machine watch or by a later resolver change.
    # The watched set keeps one watch per held host. Both guarded by ``_push_lock``.
    _hosts_awaiting_machine: dict[HostId, AgentId] = PrivateAttr(default_factory=dict)
    _hosts_watched_for_machine: set[HostId] = PrivateAttr(default_factory=set)
    _push_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Set by :meth:`stop` and, as a child of the group's shutdown event, by the group's shutdown; every machine
    # watch waits on it alone, so a watch ends with the subscription or the app without waiting out a poll.
    _stop_event: ShutdownEvent = PrivateAttr()

    def model_post_init(self, __context: object) -> None:
        self._stop_event = ShutdownEvent.from_parent(self.concurrency_group.shutdown_event)

    def start(self) -> None:
        """Subscribe to the resolver's change stream.

        Fires :meth:`_on_change` once synchronously so any agents already
        in the resolver at startup (e.g. discovered before the lifespan
        finished wiring everything together) get registered without
        waiting for the next discovery tick.
        """
        self.backend_resolver.add_on_change_callback(self._on_change)
        self._on_change()

    def stop(self) -> None:
        """Unsubscribe from the resolver and end every machine watch; a push already running finishes."""
        self.backend_resolver.remove_on_change_callback(self._on_change)
        self._stop_event.set()

    def _on_change(self) -> None:
        """Resolver change callback: register any newly-discovered agents, and retry the pushes held for a machine
        whose watch has ended."""
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
        right outcome for a host whose gateway reads the file here. While a
        host's machine watch runs, the watch carries the push instead.
        """
        with self._push_lock:
            held = [
                (host_id, agent_id)
                for host_id, agent_id in self._hosts_awaiting_machine.items()
                if host_id not in self._hosts_watched_for_machine
            ]
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
        """Mark ``host_id``'s file as changed and make sure a worker will carry it over.

        A group that can no longer run work (the app shutting down) gets no
        worker: the change stays marked and the worker slot is released, so
        nothing escapes onto the resolver's callback thread or the watch's.
        """
        with self._push_lock:
            self._hosts_awaiting_machine.pop(host_id, None)
            self._hosts_awaiting_push[host_id] = agent_id
            if host_id in self._hosts_being_pushed:
                return
            self._hosts_being_pushed.add(host_id)
        is_started = self._start_thread_unless_group_is_ending(
            target=self._push_until_settled,
            args=(host_id,),
            name=f"latchkey-auto-register-push-{host_id}",
        )
        if not is_started:
            with self._push_lock:
                self._hosts_being_pushed.discard(host_id)

    def _start_thread_unless_group_is_ending(
        self, target: Callable[..., None], args: tuple[object, ...], name: str
    ) -> bool:
        """Start ``target`` on the group, unchecked; ``False`` when the group can no longer run work.

        An exited group raises ``ConcurrencyGroupError``, one that is shutting
        down raises ``ConcurrencyExceptionGroup``, and the interpreter on its
        way out ``RuntimeError``/``OSError`` -- the families every thread start
        on the app's group names. Any of them means the app is shutting down.
        """
        try:
            start_mngr_thread(self.concurrency_group, target=target, args=args, name=name, is_checked=False)
        except (OSError, RuntimeError, ConcurrencyGroupError, ConcurrencyExceptionGroup) as e:
            logger.debug("Started no thread {}: the app is shutting down ({})", name, e)
            return False
        return True

    def _push_until_settled(self, host_id: HostId) -> None:
        """Push ``host_id``'s policy to its machine until no change is left waiting.

        Each push reads the canonical file at push time, so the last one to run
        carries everything registered up to then.

        A host with no machine yet keeps its change held rather than pushing
        into nothing: the push would be reported done while the machine, once
        provisioned, enforced the default it was seeded with instead. A watch
        (:meth:`_watch_for_machine`) polls for the machine's key and carries
        the push when it appears; after the watch ends, a later resolver change
        does (:meth:`_collect_hosts_whose_machine_appeared`).

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
                    is_watch_needed = host_id not in self._hosts_watched_for_machine
                    if is_watch_needed:
                        self._hosts_watched_for_machine.add(host_id)
                logger.debug(
                    "Holding the latchkey permissions push for host {} until it has a machine to carry them to",
                    host_id,
                )
                if is_watch_needed:
                    self._start_machine_watch(host_id)
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

    def _start_machine_watch(self, host_id: HostId) -> None:
        """Start the watch for ``host_id``'s machine; a group that is already ending (the app shutting down) gets
        none, and the push stays held for the resolver-change path."""
        is_started = self._start_thread_unless_group_is_ending(
            target=self._watch_for_machine,
            args=(host_id,),
            name=f"latchkey-auto-register-machine-watch-{host_id}",
        )
        if not is_started:
            with self._push_lock:
                self._hosts_watched_for_machine.discard(host_id)

    def _watch_for_machine(self, host_id: HostId) -> None:
        """Poll for ``host_id``'s machine key while its push is held, and carry the push once the key is there.

        The watch is bounded by ``machine_watch_seconds``: the provisioning pass
        that records the key runs seconds after the host file lands, and a host
        that never gets a key (a local one) must not be polled for the app's
        lifetime. A push still held when the watch ends waits for a resolver
        change, as before the watch existed. The watch also ends with
        :meth:`stop` and with the group's shutdown.

        Scheduling the push takes it out of the held set, so the poll after a
        carry is what ends the watch (:meth:`_held_agent_after_a_poll`) --
        unless the worker has held the push again by then, in which case this
        watch goes on polling for it.
        """
        deadline = time.monotonic() + self.machine_watch_seconds
        while (agent_id := self._held_agent_after_a_poll(host_id, deadline)) is not None:
            if self._has_machine(host_id):
                logger.debug(
                    "Carrying the held latchkey permissions push for host {}: its machine key appeared", host_id
                )
                self._schedule_push(host_id, agent_id)

    def _held_agent_after_a_poll(self, host_id: HostId, deadline: float) -> AgentId | None:
        """Wait one poll interval, then the agent ``host_id``'s held push is addressed by while its watch goes on,
        or ``None`` once the watch is over: the push is no longer held, :meth:`stop` or the group's shutdown
        happened, or ``deadline`` passed.

        This lock hold is the only place a watch leaves the watched set, and it
        does so with nothing of the watch's left to run: a resolver change or a
        worker that finds the host held and unwatched can rely on no watch
        acting on it, and a worker that re-holds the host from here on starts a
        new watch instead of relying on this one, which is about to exit.
        """
        self._stop_event.wait(timeout=self.machine_poll_interval_seconds)
        with self._push_lock:
            agent_id = self._hosts_awaiting_machine.get(host_id)
            is_over = agent_id is None or self._stop_event.is_set() or time.monotonic() >= deadline
            if is_over:
                self._hosts_watched_for_machine.discard(host_id)
                return None
            return agent_id

    def _claim_pending_push(self, host_id: HostId) -> AgentId | None:
        """Take the change waiting for ``host_id``, releasing the host's worker when there is none."""
        with self._push_lock:
            agent_id = self._hosts_awaiting_push.pop(host_id, None)
            if agent_id is None:
                self._hosts_being_pushed.discard(host_id)
            return agent_id
