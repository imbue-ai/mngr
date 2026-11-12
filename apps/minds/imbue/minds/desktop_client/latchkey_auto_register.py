"""Keep each host's latchkey permissions file registering the agents discovery lists on it.

Wires a callback onto :class:`MngrCliBackendResolver` that watches the
discovery stream and, for every host Imbue Studio manages (one whose
``latchkey_permissions.json`` exists), pins the
``minds-api-proxy-per-agent-unauthorized`` ``not.anyOf`` allowlist to the
agents the host runs, so the gateway's ``minds-api-proxy`` extension accepts
each live agent's ``/api/v1/agents/<agent_id>/...`` calls and refuses a
destroyed one's. Nothing else ever takes an agent out of that list, and a list
that only grew would make the policy outgrow the one size-capped remote command
that carries it to the host's machine.

A host is reconciled only when discovery has something new to say about it:
when its listing differs from the one last reconciled against, or when a newer
snapshot has been applied while a drop is pending (below). Resolver changes
fire far more often than that (on every service change, on every
``notify_change``), and between two snapshots the listing is stale: an agent
registered by something other than discovery (a permission request's file
repair, ``mngr latchkey register-agent``) is not in it yet.

Adding is immediate; dropping waits. A listing that includes the host's
system-services agent is complete (the criterion the resolver's last-good
topology uses too), and only a complete listing can say an agent is gone; one
without it (a stopped host, a partial read) only ever adds. Even a complete
listing can be wrong once, for an agent whose record could not be read that
cycle, so a registered agent is dropped only when two consecutive complete
listings, from two different snapshots, both lack it. A dropped agent's calls
are refused until it is registered again, so a wrong drop would cost a
workspace a discovery cycle of refused calls, and the second look is what keeps
a single bad read from costing it that.

A host whose file is not there yet is retried on later resolver changes rather
than dropped: on a brand-new workspace, discovery beats the file into
existence.

A reconcile that changes the host file is also carried to the host's own
machine, when it has one: a remote workspace's gateway enforces its own copy of
the policy, and the baseline the edit brings up to date is checked there. The
machine owns that policy, and another of the user's computers may have granted
something to it that this computer's copy has never seen, so the carry is a
read-modify-write rather than a push of the copy here: the machine's policy is
read and adopted first, the registered-agent list is pinned again on that fresh
copy (the listed agents added, the ones the reconcile dropped taken out, and
any other agent the machine registers -- a registration another computer
carried to it that discovery here has yet to list -- kept, as a registered
agent is until two complete listings lack it), and only then is the result
pushed. Pushing the copy here as it stood would replace the machine's grants
and registrations with whatever this computer last knew. The
carry runs off the resolver's callback thread (which must stay non-blocking)
and is coalesced per host, so a burst of changes costs one read and one write
that carry the latest pinned list.

A push for a host whose machine is not provisioned yet is held, not dropped.
On a brand-new remote workspace the first reconcile beats the provisioning pass
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

import functools
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import find_host_system_services_agent
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.utils.thread_cleanup import start_mngr_thread
from imbue.mngr_latchkey.agent_setup import list_registered_agent_ids_for_host
from imbue.mngr_latchkey.agent_setup import set_registered_agents_for_host
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

# How many times a carry reads its host's machine before pinning (see ``_carry_pinned_agents_to_machine``): a read
# whose adopt landed while a reconcile was rewriting the file is repeated, and a host whose listing changes faster
# than its machine can be read is pinned on the last read all the same rather than read forever.
_MAX_MACHINE_READS_PER_CARRY: Final[int] = 3


class _HostListing(FrozenModel):
    """What one discovery snapshot says about a host: the agents on it, and whether that is all of them."""

    agent_ids: frozenset[AgentId] = Field(description="Every agent the snapshot lists on the host; never empty.")
    services_agent_id: AgentId | None = Field(
        description=(
            "The host's system-services agent when the snapshot lists it, which is when the listing is complete; "
            "``None`` for a listing that may be missing agents."
        )
    )

    @property
    def is_complete(self) -> bool:
        return self.services_agent_id is not None

    @property
    def push_address(self) -> AgentId:
        """The agent a push of the host's policy is addressed by: any listed one resolves to the host."""
        return self.services_agent_id if self.services_agent_id is not None else min(self.agent_ids)


class _Reconciliation(FrozenModel):
    """What this computer last pinned a host's registered agents against."""

    listing: frozenset[AgentId] = Field(description="The agents the listing had.")
    snapshot_count: int = Field(
        description=(
            "The resolver's count of applied snapshots when the listing was read, so a later listing is known to "
            "come from a newer snapshot even when it reads the same."
        )
    )
    absent_registered: frozenset[AgentId] = Field(
        description="The registered agents the last complete listing lacked, dropped if the next one lacks them too."
    )


class _ReconciliationPlan(FrozenModel):
    """What one reconcile of a host does to its registered agents, and what it leaves for the next one."""

    target_agent_ids: frozenset[AgentId] = Field(description="The agents the host's file is pinned to.")
    dropped_agent_ids: frozenset[AgentId] = Field(description="The registered agents this reconcile drops.")
    absent_registered: frozenset[AgentId] = Field(
        description="The registered agents kept although the listing lacked them, for the next reconcile to judge."
    )


class _PendingCarry(FrozenModel):
    """A reconcile's pin waiting to be applied again on the machine's policy and carried back to it."""

    address_agent_id: AgentId = Field(description="The agent the machine operations are addressed by.")
    listed_agent_ids: frozenset[AgentId] = Field(
        description="The agents discovery listed on the host; all registered."
    )
    dropped_agent_ids: frozenset[AgentId] = Field(
        description=(
            "The registered agents the reconciles behind this carry dropped; taken out of the machine's policy too."
        )
    )


@pure
def _pinned_agent_ids(
    registered: frozenset[AgentId], listed: frozenset[AgentId], dropped: frozenset[AgentId]
) -> frozenset[AgentId]:
    """The agents a host's file is pinned to: every registered one not dropped, plus every listed one."""
    return (registered - dropped) | listed


@pure
def _coalesced_carry(earlier: _PendingCarry, newer: _PendingCarry) -> _PendingCarry:
    """One carry for two reconciles of a host: the newer address, what either one listed, and what either one dropped.

    Only the reconcile that dropped an agent knows it did (a later one ran on
    a file already without it), and the machine's policy, read back before
    the pin, still registers it; so a carry replaced before it ran would have
    let the agent live on there. The earlier listing is kept too, minus what
    the newer carry dropped: an agent only the earlier listing had, which the
    newer complete listing lacked for the first time, is still registered in
    the file here (a registered agent is dropped only once two complete
    listings lack it), and the machine never received the earlier carry, so a
    pin of the newer listing alone would unregister it after one absence. The
    pin registers the listed agents after taking the dropped ones out, so an
    agent the earlier reconcile dropped and the newer one lists again is
    registered all the same.
    """
    return _PendingCarry(
        address_agent_id=newer.address_agent_id,
        listed_agent_ids=(earlier.listed_agent_ids - newer.dropped_agent_ids) | newer.listed_agent_ids,
        dropped_agent_ids=earlier.dropped_agent_ids | newer.dropped_agent_ids,
    )


@pure
def _folded_carries(earlier_oldest_first: Sequence[_PendingCarry | None], newest: _PendingCarry) -> _PendingCarry:
    """The one carry a host's carries fold into (:func:`_coalesced_carry`): the earlier ones, oldest first, then the newest.

    ``None`` stands for a slot with no carry in it. Every place that folds
    carries goes through this, so the age order the fold depends on is
    written in one place.
    """
    return functools.reduce(
        _coalesced_carry, [*(carry for carry in earlier_oldest_first if carry is not None), newest]
    )


@pure
def _host_listings(agents: Sequence[DiscoveredAgent]) -> dict[HostId, _HostListing]:
    agents_by_host: dict[HostId, list[DiscoveredAgent]] = {}
    for agent in agents:
        agents_by_host.setdefault(agent.host_id, []).append(agent)
    listings: dict[HostId, _HostListing] = {}
    for host_id, host_agents in agents_by_host.items():
        services_agent = find_host_system_services_agent(host_agents)
        listings[host_id] = _HostListing(
            agent_ids=frozenset(agent.agent_id for agent in host_agents),
            services_agent_id=None if services_agent is None else services_agent.agent_id,
        )
    return listings


@pure
def _is_reconcile_due(last: _Reconciliation, listing: _HostListing, snapshot_count: int) -> bool:
    """Whether discovery has said something new about a host since it was last reconciled.

    A changed listing is new. So is an unchanged one from a newer snapshot,
    but only while a drop is pending on it: that second look is what the
    drop waits for, and reading the file on every snapshot otherwise would
    buy nothing.
    """
    if last.listing != listing.agent_ids:
        return True
    return bool(last.absent_registered) and snapshot_count > last.snapshot_count


@pure
def _plan_reconciliation(
    registered: frozenset[AgentId], listing: _HostListing, last: _Reconciliation | None
) -> _ReconciliationPlan:
    """Decide the agents a host's file is pinned to, given what is registered and what discovery lists.

    Every listed agent is registered. A registered agent the listing lacks is
    dropped only when the listing is complete and the previous complete
    listing lacked it too; otherwise it is kept, and remembered as absent
    when the listing is complete. A listing that is not complete carries the
    remembered absences forward, minus any agent it lists again.
    """
    previously_absent = frozenset() if last is None else last.absent_registered
    if not listing.is_complete:
        return _ReconciliationPlan(
            target_agent_ids=_pinned_agent_ids(registered, listing.agent_ids, frozenset()),
            dropped_agent_ids=frozenset(),
            absent_registered=previously_absent - listing.agent_ids,
        )
    absent = registered - listing.agent_ids
    dropped = absent & previously_absent
    return _ReconciliationPlan(
        target_agent_ids=_pinned_agent_ids(registered, listing.agent_ids, dropped),
        dropped_agent_ids=dropped,
        absent_registered=absent - dropped,
    )


class LatchkeyAutoRegister(MutableModel):
    """Subscribes to ``MngrCliBackendResolver`` and pins each managed host's registered agents to the ones it runs.

    A reconcile reads the host file, decides the new list and writes it back,
    and change callbacks fire from several threads at once, so every reconcile
    runs under ``_lock``, listing read included: a reconcile's listing is then
    at least as new as the one behind any reconcile before it.
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
    refresh_permissions_from_machine: Callable[[str], None] = Field(
        frozen=True,
        description=(
            "Makes this computer's copy of a workspace's policy say what its machine holds, given the "
            "workspace's agent id; a no-op for a workspace with no machine of its own. Raises "
            "``MachineOperationError`` when the machine cannot be read."
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

    # What each host was last reconciled against, so a resolver change that
    # tells nothing new about a host costs no read of its file. Guarded by ``_lock``.
    _reconciliation_by_host: dict[HostId, _Reconciliation] = PrivateAttr(default_factory=dict)
    # How many times a reconcile has written each host's file, so a carry can
    # tell whether one wrote while its machine was being read. Guarded by ``_lock``.
    _reconcile_write_count_by_host: dict[HostId, int] = PrivateAttr(default_factory=dict)
    # Hosts found without a permissions file, so the deferral is logged once per
    # host rather than on every resolver change. Guarded by ``_lock``.
    _hosts_awaiting_file: set[HostId] = PrivateAttr(default_factory=set)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    # Hosts whose file changed since their machine last received it, each with
    # the list it was pinned to and an agent on it (how the carry addresses the
    # workspace), and the hosts a push worker is currently running for. Both
    # guarded by ``_push_lock``: the worker keeps going while its host is
    # re-marked, so a change made mid-push is carried by the next iteration
    # rather than by a second, racing thread.
    _hosts_awaiting_push: dict[HostId, _PendingCarry] = PrivateAttr(default_factory=dict)
    _hosts_being_pushed: set[HostId] = PrivateAttr(default_factory=set)
    # Hosts whose changed file found no machine to go to yet, each with the
    # carry that is held; re-scheduled once the machine has been provisioned,
    # by the host's machine watch or by a later resolver change. The watched
    # set keeps one watch per held host. Both guarded by ``_push_lock``.
    _hosts_awaiting_machine: dict[HostId, _PendingCarry] = PrivateAttr(default_factory=dict)
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
        """Resolver change callback: reconcile the hosts discovery said something new about, and retry the carries
        held for a machine whose watch has ended."""
        for host_id, pending in self._reconcile_hosts():
            self._schedule_push(host_id, pending)
        for host_id in self._collect_hosts_whose_machine_appeared():
            self._carry_held(host_id)

    def _reconcile_hosts(self) -> list[tuple[HostId, _PendingCarry]]:
        """Pin the file of every host discovery said something new about; the hosts whose file changed, each with
        the carry that takes the pinned list to its machine.

        A destroyed host that still lingers in the snapshot is left alone, since
        its policy is nobody's to enforce any more. A host without a file is
        not recorded as reconciled, so it is looked at again on the next
        resolver change (one ``stat`` each) and reconciled against the listing
        of the moment its file appears.
        """
        pushes: list[tuple[HostId, _PendingCarry]] = []
        with self._lock:
            snapshot = self.backend_resolver.get_discovered_agents_snapshot()
            for host_id, listing in _host_listings(snapshot.agents).items():
                if self.backend_resolver.get_host_state(host_id) is HostState.DESTROYED:
                    continue
                last = self._reconciliation_by_host.get(host_id)
                if last is not None and not _is_reconcile_due(last, listing, snapshot.snapshot_count):
                    continue
                if not permissions_path_for_host(self.latchkey.plugin_data_dir, host_id).is_file():
                    if host_id not in self._hosts_awaiting_file:
                        self._hosts_awaiting_file.add(host_id)
                        logger.debug(
                            "Deferring the latchkey registration of host {}: no permissions file yet", host_id
                        )
                    continue
                self._hosts_awaiting_file.discard(host_id)
                plan = self._reconcile_locked(host_id, listing, last, snapshot.snapshot_count)
                if plan is not None:
                    pushes.append(
                        (
                            host_id,
                            _PendingCarry(
                                address_agent_id=listing.push_address,
                                listed_agent_ids=listing.agent_ids,
                                dropped_agent_ids=plan.dropped_agent_ids,
                            ),
                        )
                    )
        return pushes

    def _reconcile_locked(
        self, host_id: HostId, listing: _HostListing, last: _Reconciliation | None, snapshot_count: int
    ) -> _ReconciliationPlan | None:
        """Pin ``host_id``'s registered agents per :func:`_plan_reconciliation`; the plan when the file changed,
        else ``None``.

        A file that cannot be read or written is logged and recorded as
        reconciled against this listing all the same, so it is tried again
        when discovery next says something new about the host rather than on
        every resolver change. Must be called with ``_lock`` held.
        """
        try:
            registered = list_registered_agent_ids_for_host(self.latchkey.plugin_data_dir, host_id)
            plan = _plan_reconciliation(registered, listing, last)
            is_file_changed = set_registered_agents_for_host(
                self.latchkey.plugin_data_dir, host_id, plan.target_agent_ids
            )
        except (LatchkeyStoreError, OSError) as e:
            logger.warning(
                "Failed to pin the registered agents of host {} to the ones discovery lists: {}", host_id, e
            )
            self._reconciliation_by_host[host_id] = _Reconciliation(
                listing=listing.agent_ids, snapshot_count=snapshot_count, absent_registered=frozenset()
            )
            return None
        self._reconciliation_by_host[host_id] = _Reconciliation(
            listing=listing.agent_ids, snapshot_count=snapshot_count, absent_registered=plan.absent_registered
        )
        if not is_file_changed:
            return None
        self._reconcile_write_count_by_host[host_id] = self._reconcile_write_count_by_host.get(host_id, 0) + 1
        return plan

    def _collect_hosts_whose_machine_appeared(self) -> list[HostId]:
        """The hosts with a held carry that now have a machine of their own.

        One ``stat`` of the host's machine key per held host per resolver
        change, like a file-less host's ``stat`` of its permissions file --
        and, like that, paid for as long as the host has no machine: a local
        host never gets one, so its held push is never carried, which is the
        right outcome for a host whose gateway reads the file here. While a
        host's machine watch runs, the watch carries the push instead.
        """
        with self._push_lock:
            held = [
                host_id for host_id in self._hosts_awaiting_machine if host_id not in self._hosts_watched_for_machine
            ]
        return [host_id for host_id in held if self._has_machine(host_id)]

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

    def _schedule_push(self, host_id: HostId, pending: _PendingCarry) -> None:
        """Mark ``host_id``'s pinned list as waiting and make sure a worker will carry it over.

        A carry already held or waiting for the host is folded into this one
        (:func:`_coalesced_carry`) rather than replaced, so a drop only it knew
        of is still carried. The fold is oldest first: the held carry was
        claimed before the waiting one could be scheduled, and both before
        this one.
        """
        with self._push_lock:
            self._hosts_awaiting_push[host_id] = _folded_carries(
                (self._hosts_awaiting_machine.pop(host_id, None), self._hosts_awaiting_push.get(host_id)), pending
            )
            is_worker_needed = self._claim_push_worker_locked(host_id)
        if is_worker_needed:
            self._start_push_worker(host_id)

    def _carry_held(self, host_id: HostId) -> None:
        """Put the carry held for ``host_id`` back among the waiting ones, its machine having appeared, and make
        sure a worker will carry it over.

        A carry scheduled while it was held is folded into it as the newer of
        the two (:func:`_coalesced_carry`). Nothing held -- a worker or a
        resolver change got there first -- means nothing to carry.
        """
        with self._push_lock:
            held = self._hosts_awaiting_machine.pop(host_id, None)
            if held is None:
                return
            waiting = self._hosts_awaiting_push.get(host_id)
            self._hosts_awaiting_push[host_id] = held if waiting is None else _folded_carries((held,), waiting)
            is_worker_needed = self._claim_push_worker_locked(host_id)
        if is_worker_needed:
            self._start_push_worker(host_id)

    def _claim_push_worker_locked(self, host_id: HostId) -> bool:
        """Take ``host_id``'s worker slot; ``False`` when a worker already runs for it. Must be called with
        ``_push_lock`` held."""
        if host_id in self._hosts_being_pushed:
            return False
        self._hosts_being_pushed.add(host_id)
        return True

    def _start_push_worker(self, host_id: HostId) -> None:
        """Start the worker whose slot :meth:`_claim_push_worker_locked` took.

        A group that can no longer run work (the app shutting down) gets no
        worker: the change stays marked and the worker slot is released, so
        nothing escapes onto the resolver's callback thread or the watch's.
        """
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
        """Carry ``host_id``'s pinned list to its machine until no change is left waiting.

        Each carry re-pins the list that was waiting when it ran, so the last
        one to run carries everything reconciled up to then.

        A host with no machine yet keeps its change held rather than pushing
        into nothing: the push would be reported done while the machine, once
        provisioned, enforced the default it was seeded with instead. A watch
        (:meth:`_watch_for_machine`) polls for the machine's key and carries
        the push when it appears; after the watch ends, a later resolver change
        does (:meth:`_collect_hosts_whose_machine_appeared`). A carry held
        over one already held for the host -- scheduled between this worker's
        claim and its hold, when :meth:`_schedule_push` found nothing to fold
        it into -- is folded into it here, as it would have been there.

        A machine that cannot be read, or will not take a push, is logged as
        an error and not retried here. There is no user waiting on a reconcile
        to tell, and the change does *not* heal itself later: the machine owns
        the policy, so the next read of it adopts the machine's copy over this
        one and the change is lost, until the next reconcile of the host writes
        the file again (recover sooner with ``mngr latchkey register-agent``
        once the machine is reachable).
        """
        while (pending := self._claim_pending_push(host_id)) is not None:
            if not self._has_machine(host_id):
                with self._push_lock:
                    self._hosts_awaiting_machine[host_id] = _folded_carries(
                        (self._hosts_awaiting_machine.get(host_id),), pending
                    )
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
            self._carry_pinned_agents_to_machine(host_id, pending)

    def _carry_pinned_agents_to_machine(self, host_id: HostId, pending: _PendingCarry) -> None:
        """Read the machine's policy, pin the registered agents again on it, and push the result.

        The read adopts what the machine holds over this computer's copy, so a
        grant another of the user's computers made to the machine survives the
        push; the pin is what this reconcile set out to do, re-applied to that
        fresh copy: the listed agents registered, the dropped ones taken out,
        and an agent the machine registers that this computer's copy never held
        kept, since only two complete listings lacking it may drop it. A
        machine whose copy already pins the same list is left alone, since a
        push would change nothing there.
        """
        is_file_changed = self._pin_on_the_machines_policy(host_id, pending)
        if is_file_changed is None:
            return
        if not is_file_changed:
            logger.debug("The machine of host {} already registers the pinned agents; nothing to push", host_id)
            return
        try:
            self.push_permissions_to_machine(str(pending.address_agent_id))
        except MachineOperationError as e:
            logger.error(
                "Failed to push the latchkey permissions of host {} to its machine (addressed by agent {}); "
                "the change to the registered agents will be lost when the machine is next read: {}",
                host_id,
                pending.address_agent_id,
                e,
            )

    def _pin_on_the_machines_policy(self, host_id: HostId, pending: _PendingCarry) -> bool | None:
        """Read the machine's policy and pin the registered agents again on it; whether the file changed, or
        ``None`` when the machine could not be read or the pin failed (each logged where it happened).

        The read runs outside ``_lock``, since it is a network round trip the
        resolver's callback thread must not wait on, so a reconcile can rewrite
        the file while it is in flight: one that read the file before the
        adopt landed and wrote after it has put the stale copy back over the
        machine's policy. A read a reconcile wrote during is therefore
        repeated (``_MAX_MACHINE_READS_PER_CARRY`` times at most) before the
        pin, so what is pinned and pushed is what the machine holds.
        """
        address = str(pending.address_agent_id)
        is_file_changed: bool | None = None
        for read_count in range(1, _MAX_MACHINE_READS_PER_CARRY + 1):
            with self._lock:
                reconcile_writes_before_read = self._reconcile_write_count_by_host.get(host_id, 0)
            try:
                self.refresh_permissions_from_machine(address)
            except MachineOperationError as e:
                logger.error(
                    "Failed to read the latchkey permissions of host {} from its machine (addressed by agent {}) "
                    "before pushing the registered agents; the change will be lost when the machine is next read: {}",
                    host_id,
                    address,
                    e,
                )
                return None
            with self._lock:
                is_read_undisturbed = (
                    self._reconcile_write_count_by_host.get(host_id, 0) == reconcile_writes_before_read
                )
                if is_read_undisturbed or read_count == _MAX_MACHINE_READS_PER_CARRY:
                    if not is_read_undisturbed:
                        logger.warning(
                            "A reconcile of host {} wrote its permissions file during each of {} reads of its "
                            "machine; pinning the registered agents on the last read",
                            host_id,
                            read_count,
                        )
                    is_file_changed = self._pin_registered_agents_locked(host_id, pending)
                    break
            logger.debug(
                "A reconcile of host {} wrote its permissions file while its machine was being read; reading it again",
                host_id,
            )
        return is_file_changed

    def _pin_registered_agents_locked(self, host_id: HostId, pending: _PendingCarry) -> bool | None:
        """Pin the registered agents of ``host_id``'s freshly adopted copy per ``pending``; whether the file
        changed, or ``None`` when it could not be pinned. Must be called with ``_lock`` held."""
        try:
            registered = list_registered_agent_ids_for_host(self.latchkey.plugin_data_dir, host_id)
            return set_registered_agents_for_host(
                self.latchkey.plugin_data_dir,
                host_id,
                _pinned_agent_ids(registered, pending.listed_agent_ids, pending.dropped_agent_ids),
            )
        except (LatchkeyStoreError, OSError) as e:
            logger.error(
                "Failed to pin the registered agents of host {} on the policy read from its machine; the copy "
                "here now says what the machine holds, so the change to the registered agents is lost until "
                "discovery next says something new about the host: {}",
                host_id,
                e,
            )
            return None

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

        Carrying the push takes it out of the held set, so the poll after a
        carry is what ends the watch (:meth:`_is_held_after_a_poll`) -- unless
        the worker has held the push again by then, in which case this watch
        goes on polling for it.
        """
        deadline = time.monotonic() + self.machine_watch_seconds
        while self._is_held_after_a_poll(host_id, deadline):
            if self._has_machine(host_id):
                logger.debug(
                    "Carrying the held latchkey permissions push for host {}: its machine key appeared", host_id
                )
                self._carry_held(host_id)

    def _is_held_after_a_poll(self, host_id: HostId, deadline: float) -> bool:
        """Wait one poll interval, then whether ``host_id``'s push is still held and its watch goes on; ``False``
        once the watch is over: the push is no longer held, :meth:`stop` or the group's shutdown happened, or
        ``deadline`` passed.

        This lock hold is the only place a watch leaves the watched set, and it
        does so with nothing of the watch's left to run: a resolver change or a
        worker that finds the host held and unwatched can rely on no watch
        acting on it, and a worker that re-holds the host from here on starts a
        new watch instead of relying on this one, which is about to exit.
        """
        self._stop_event.wait(timeout=self.machine_poll_interval_seconds)
        with self._push_lock:
            is_over = (
                host_id not in self._hosts_awaiting_machine
                or self._stop_event.is_set()
                or time.monotonic() >= deadline
            )
            if is_over:
                self._hosts_watched_for_machine.discard(host_id)
            return not is_over

    def _claim_pending_push(self, host_id: HostId) -> _PendingCarry | None:
        """Take the change waiting for ``host_id``, releasing the host's worker when there is none."""
        with self._push_lock:
            pending = self._hosts_awaiting_push.pop(host_id, None)
            if pending is None:
                self._hosts_being_pushed.discard(host_id)
            return pending
