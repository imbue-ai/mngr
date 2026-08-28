"""Laptop-side environment signals: sleep, and connectivity from this device.

Minds convicts a workspace of being stuck by watching it fail to answer, and
then acts on that conviction by restarting it. Both steps quietly assume the
laptop was awake and on a working network the whole time. On a laptop neither
holds often enough to matter: the lid closes mid-outage-check and every
background loop stops dead; the wifi drops on a train and every remote machine
stops answering at once; a corporate or hotel network passes HTTP but blocks
outbound SSH, so the browser works while nothing minds does can reach a host.
Each of those reads, through the machinery downstream, as the workspace dying.

This module supplies the two facts that separate the device's condition from the
workspace's: which wall-clock windows this process spent not running
(:class:`SleepTracker`), and whether this device can currently reach anything at
all (:class:`ConnectivityDetector`). Neither ever asserts health. A signal here
may suppress a negative verdict or withhold an action; nothing here can make a
workspace read as healthy, and an *unknown* reading -- the state right after a
wake, before any probe has landed -- suppresses nothing at all. Workspaces
dialled on this device are exempt from the connectivity signals entirely (which
machines those are is decided from the address minds connects to, by the
recovery paths that apply the rule); the sleep signal still applies to them,
because the probe loop was frozen regardless of where the machine lives.

Sleep is detected by heartbeat rather than by a platform sleep/wake
notification. ``SleepTracker`` ticks about once a second and compares
consecutive wall-clock readings; a gap far larger than the tick is a window in
which the loop was not scheduled. That covers system sleep, and equally covers a
process suspended by anything else (SIGSTOP, a hypervisor pause), which is the
same fact for every consumer here.

The Electron shell is the road not taken, deliberately. It already subscribes to
``powerMonitor``'s ``resume`` and ``unlock-screen`` (``electron/main.js``), and
relaying those to this process would be a handful of lines. Three reasons not
to: that signal names the wake but not the window, so the tracker would still
need its own record of when the process stopped; it does not exist at all in
browser mode or for any of the non-sleep suspensions above; and it would make
a laptop-side correctness property depend on an IPC hop that can be missed,
where the heartbeat's absence *is* the evidence. Worth revisiting only if the
heartbeat proves noisy in the field, and then as corroboration rather than as a
replacement.

The heartbeat can be trusted to bound *elapsed* time because ``time.monotonic``
cannot: it freezes across sleep on macOS (a measured ~1-3s of advance across two
~15-minute lid-closed sleeps on Apple Silicon), so a monotonic deadline set
before the lid closed silently extends itself across the sleep. Wall clock is
the only reading that spans it. The two are recorded together anyway, because
their difference is what distinguishes a real sleep (wall advanced, monotonic
did not) from a merely-starved process (both advanced), and that distinction is
worth having in the log when a gap is being explained after the fact.

Connectivity is measured on demand and never in the background. Nothing here
polls the network to keep a reading warm: a probe runs when some consumer is
about to act on the answer, and then repeatedly only while a bad reading is
outstanding, until it clears. Steady state is silence.

The SSH facet asks about the endpoints minds itself dials, supplied by whoever
constructs the detector. It has to: minds' machines are not on port 22 -- an
imbue_cloud host answers on a port its box forwarded, somewhere in the
22000-32000 range -- so a public :22 check would measure a port those machines
never use, in both directions. Public SSH is still probed, but only to break the
tie when none of minds' own endpoints answer, which is the one case the
endpoints cannot resolve alone: every machine failing is either the network or
those machines, and a public host still serving SSH says it was the machines.
"""

import socket
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SkipValidation

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.event_utils import ReadOnlyEvent
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import MindError
from imbue.mngr.errors import MngrError

# How large a wall-clock gap between consecutive heartbeats must be before the
# window between them counts as time this process was not running. Thirty times
# the tick interval, so ordinary scheduling hiccups -- a GIL-bound thread, a
# loaded machine, a slow garbage collection -- can never be mistaken for a sleep,
# while the shortest sleep a user can actually take still clears it.
_DEFAULT_HEARTBEAT_GAP_THRESHOLD_SECONDS: Final[float] = 30.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SleepInterval(FrozenModel):
    """A wall-clock window during which this process was observed not to run.

    The bounds are the two heartbeats that straddle the gap, so the real sleep
    is contained within them: it began at or after ``started_at`` (the last tick
    before the machine went down) and ended at or before ``ended_at`` (the first
    tick after it came back). Both are inclusive of at most one tick of slack,
    which is why consumers treat the interval as "not awake" rather than as a
    measurement of how long the machine slept.
    """

    started_at: datetime = Field(description="Wall-clock (UTC) time of the last heartbeat before the gap")
    ended_at: datetime = Field(description="Wall-clock (UTC) time of the first heartbeat after the gap")


# Fired with the moment the process was first observed running again.
OnWakeCallback = Callable[[datetime], None]


class SleepTracker(MutableModel):
    """Records the wall-clock windows in which this process was not running.

    Construct one per minds process and drive it from a background loop that
    calls :meth:`record_heartbeat` about once a second (see
    ``start_sleep_heartbeat_loop`` in ``app.py``). Consumers ask it two things:
    whether a stretch they have been measuring straddles a sleep
    (:meth:`was_asleep_since`), and when the process most recently came back
    (:meth:`get_last_wake_at`), which is the baseline for anything that ages a
    timestamp against now.

    Every reading is negative-only by construction. No interval recorded -- a
    process that just started, or one whose heartbeat loop is not running --
    answers "no sleep recorded", which leaves every consumer exactly as it
    behaves today.
    """

    heartbeat_gap_threshold_seconds: float = Field(
        default=_DEFAULT_HEARTBEAT_GAP_THRESHOLD_SECONDS,
        description="Wall-clock gap between heartbeats above which the window counts as a sleep interval.",
    )
    now_fn: Callable[[], datetime] = Field(
        default=_utc_now,
        description="Injectable UTC wall clock (overridden in tests for deterministic gaps).",
    )
    monotonic_fn: Callable[[], float] = Field(
        default=time.monotonic,
        description="Injectable monotonic clock, recorded alongside wall time to label a gap in the log.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Only the most recent one: see :meth:`was_asleep_since` for why no consumer
    # can need an older one, which is what keeps this bounded without a sweep.
    _last_interval: SleepInterval | None = PrivateAttr(default=None)
    _last_heartbeat_wall_at: datetime | None = PrivateAttr(default=None)
    _last_heartbeat_monotonic_at: float | None = PrivateAttr(default=None)
    _on_wake_callbacks: list[OnWakeCallback] = PrivateAttr(default_factory=list)

    # -- Public callback registration -------------------------------------

    def add_on_wake_callback(self, callback: OnWakeCallback) -> None:
        """Register a callback fired once per recorded sleep interval, with its end.

        Callbacks run outside this tracker's lock, so they may take other
        components' locks -- but not on any particular thread. They run on
        whichever loop's tick happened to close the gap: the heartbeat loop, or
        one of the consumers that establishes the wake for itself rather than
        race the heartbeat for it (the discovery watchdog, the system-interface
        health probe). So keep them fast for the reason that survives whichever
        it was -- what a slow callback delays is that loop's own next act, which
        may be a stall verdict or a pass of the loop that adjudicates STUCK.
        """
        with self._lock:
            self._on_wake_callbacks.append(callback)

    # -- Input ------------------------------------------------------------

    def record_heartbeat(self) -> None:
        """Record that the process is running now, opening an interval if it was not.

        The whole detector: compare this tick's wall clock against the previous
        tick's, and treat a gap past the threshold as a window in which nothing
        here ran. Idempotent in the sense that matters -- an extra tick can only
        shorten the gap it measures, never invent one.

        A gap that runs *backwards* (the wall clock stepped back, e.g. an NTP
        correction) records nothing: it is not evidence the process stopped, and
        an interval whose end precedes its start would make every overlap check
        that reads it nonsense. The baseline still advances to the new reading,
        so the next tick measures against a clock that agrees with the one it
        will sample.
        """
        interval: SleepInterval | None = None
        monotonic_gap_seconds = 0.0
        callbacks: list[OnWakeCallback] = []
        with self._lock:
            # Sampled under the lock: several loops tick this, and a tick that
            # read the clock before a sleep and took the lock after one would
            # otherwise write its pre-sleep reading back over the baseline the
            # wake had just moved -- making the next tick measure the same sleep
            # a second time, and fire a second wake for it.
            now_wall = self.now_fn()
            now_monotonic = self.monotonic_fn()
            previous_wall = self._last_heartbeat_wall_at
            previous_monotonic = self._last_heartbeat_monotonic_at
            self._last_heartbeat_wall_at = now_wall
            self._last_heartbeat_monotonic_at = now_monotonic
            if previous_wall is not None and previous_monotonic is not None:
                wall_gap_seconds = (now_wall - previous_wall).total_seconds()
                if wall_gap_seconds >= self.heartbeat_gap_threshold_seconds:
                    interval = SleepInterval(started_at=previous_wall, ended_at=now_wall)
                    monotonic_gap_seconds = now_monotonic - previous_monotonic
                    self._last_interval = interval
                    callbacks = list(self._on_wake_callbacks)
        if interval is None:
            return
        # The one line that explains every downstream suppression, and the one
        # place the two clocks are compared: a wall gap the monotonic clock did
        # not see is a machine that slept, while a gap both clocks saw is a
        # process that was merely starved of CPU for that long. Both suppress the
        # same way (nothing here ran either way), so the difference is reported
        # rather than branched on.
        wall_gap_seconds = (interval.ended_at - interval.started_at).total_seconds()
        logger.info(
            "Sleep interval recorded: {:.0f}s of wall clock passed between heartbeats "
            "(monotonic advanced {:.0f}s, frozen for {:.0f}s), from {} to {}",
            wall_gap_seconds,
            monotonic_gap_seconds,
            wall_gap_seconds - monotonic_gap_seconds,
            interval.started_at.isoformat(),
            interval.ended_at.isoformat(),
        )
        # At least as wide as the connectivity fence, which names the same two
        # families for the same reason (a MindError is a ClickException, not a
        # RuntimeError). This one guards the more valuable threads: the wake is
        # established by the discovery watchdog and the system-interface health
        # probe on their own loops, both checked strands, and the second of them
        # is the single authority on STUCK.
        for callback in callbacks:
            try:
                callback(interval.ended_at)
            except (OSError, RuntimeError, ValueError, MindError, MngrError) as e:
                logger.opt(exception=e).warning("SleepTracker on-wake callback failed: {}", e)

    # -- Readings ---------------------------------------------------------

    def was_asleep_since(self, start: datetime) -> bool:
        """Whether this process stopped running at any point between ``start`` and now.

        The question a consumer reasoning over elapsed time actually has: "was
        the whole of the stretch I have been measuring actually observed?". A
        stretch that merely *reaches into* a sleep is already disqualified --
        the seconds it claims to have watched include seconds nobody watched.

        Scoped to "since", rather than an arbitrary past window, because that is
        every consumer's question and because it is what the latest interval can
        answer on its own: intervals are recorded in order and cannot overlap, so
        one that ends before ``start`` is preceded only by intervals that end
        earlier still. A window ending in the past would need the whole history;
        nothing asks for one, and offering the parameter would invite a caller
        to get a confidently wrong answer.
        """
        with self._lock:
            interval = self._last_interval
        return interval is not None and interval.ended_at > start

    def was_asleep_during(self, start: datetime, end: datetime) -> bool:
        """Whether this process stopped running at any point in the closed window ``[start, end]``.

        For an observation that is already over when it is read -- a discovery
        poll that reports when it began and when it finished. Such a result is
        only evidence if the whole window was watched, and :meth:`was_asleep_since`
        cannot say: it would also disqualify a window that closed before the
        sleep and was merely consumed after the wake.

        Answered from the latest interval alone, which is exact for every window
        that reaches to now or into the latest sleep, and answers "no" for a
        window that ended before an earlier sleep began. That earlier window is
        one no consumer holds: the observations this is asked about are consumed
        as they arrive, so a reading old enough to predate two sleeps has long
        since been superseded.
        """
        with self._lock:
            interval = self._last_interval
        return interval is not None and interval.ended_at > start and interval.started_at < end

    def get_last_wake_at(self) -> datetime | None:
        """Wall-clock (UTC) end of the most recent sleep interval, or None if none is recorded.

        The moment this process was first seen running again -- the baseline for
        anything that ages a timestamp against now, since no timestamp older
        than this was produced by a loop that was actually running. None on a
        process that has not slept, which leaves such a consumer on whatever
        baseline it already had.
        """
        with self._lock:
            interval = self._last_interval
        return interval.ended_at if interval is not None else None


# Public hosts probed to decide whether this device can reach anything. Chosen
# for independence: three separate operators, so one of them being down (or
# blocked by a single site's policy) cannot by itself produce a verdict. The same
# three answer for both ports, which is what makes the SSH verdict possible at
# all: whichever one answered on 443 is among the ones asked on 22, so passing
# HTTPS while every one of them refuses SSH is the network blocking a port
# rather than the hosts being unreachable. Both facets ask all three together,
# for the reason :meth:`ConnectivityDetector._does_any_ssh_endpoint_answer`
# gives: the answer that costs the most is the one that has to hear from every
# host.
_PROBE_HOSTS: Final[tuple[str, ...]] = ("github.com", "gitlab.com", "bitbucket.org")
_HTTPS_PORT: Final[int] = 443
_PUBLIC_SSH_PORT: Final[int] = 22

# How many of minds' own SSH endpoints one probe will try before falling back to
# the public quorum. The question is about the network, which any one of them
# answers when it succeeds; the cap bounds the case where none of them do.
_MAX_SAMPLED_WORKSPACE_SSH_ENDPOINTS: Final[int] = 3

# Budget for reaching one endpoint, shared across every address that endpoint
# resolves to (:meth:`SocketNetworkProber._connect_within_budget`). Short because
# the whole probe is on the critical path of a recovery decision, and because a
# network that needs longer than this to answer three well-connected hosts is not
# one a workspace is reachable over either.
_DEFAULT_PROBE_TIMEOUT_SECONDS: Final[float] = 1.5

# How often a *bad* reading is re-checked, so an owed action fires soon after
# the network comes back. Only ever runs while a bad reading is outstanding.
_DEFAULT_CONNECTIVITY_POLL_INTERVAL_SECONDS: Final[float] = 5.0

# What an SSH server says first. Read rather than merely connecting, because a
# captive portal or filtering middlebox will happily accept the connection and
# then serve something that is not SSH; a connect alone would report that as a
# working SSH path.
_SSH_BANNER_PREFIX: Final[bytes] = b"SSH-2.0"


class ConnectivityFacet(UpperCaseStrEnum):
    """One measured aspect of this device's reach, or the absence of a measurement."""

    ONLINE = auto()
    OFFLINE = auto()
    # No probe has landed yet, or the last one was invalidated by a wake. Never
    # a verdict: consumers treat it exactly as they treat ONLINE.
    UNKNOWN = auto()


class EnvironmentBlock(UpperCaseStrEnum):
    """Why this *device* -- not the machine -- is why nothing can be reached.

    Carried on a workspace's health state while it applies, so the recovery
    surfaces can name the real condition instead of narrating a recovery that was
    never dispatched. ``NONE`` is the normal state and the only one that permits
    an unattended start.
    """

    NONE = auto()
    OFFLINE = auto()
    SSH_BLOCKED = auto()


class EnvironmentCondition(UpperCaseStrEnum):
    """This device's condition as the surfaces should describe it: a block, none, or not yet known.

    :class:`EnvironmentBlock` is the answer for *acting*, where an unmeasured
    device must count as fine so that nothing is ever withheld on no evidence.
    The surfaces ask a different question -- whose fault is it -- and there "no
    measurement" is not "the device is fine": a surface handed ``NONE`` for a
    reading nobody has taken goes on to blame the next thing down, which after a
    wake is the provider. ``UNKNOWN`` is that third answer, and a surface
    reading it declines to blame anyone until a probe lands.
    """

    NONE = auto()
    OFFLINE = auto()
    SSH_BLOCKED = auto()
    UNKNOWN = auto()


class SshEndpoint(FrozenModel):
    """One ``host:port`` minds opens SSH connections to."""

    host: str = Field(description="Hostname or address the SSH connection is made to")
    port: int = Field(description="Port the SSH server answers on; rarely 22 for minds' own machines")


class ConnectivityReading(FrozenModel):
    """What the last probe found, as two independent facets.

    Two facets rather than one flag because the user must not be told the wrong
    thing: on a network that blocks outbound SSH their browser works perfectly,
    so "you appear to be offline" is a claim they can see is false, and they will
    reasonably discount whatever else the app says next.
    """

    internet: ConnectivityFacet = Field(description="Whether any probe host answered on the HTTPS port")
    ssh: ConnectivityFacet = Field(
        description=(
            "Whether this device can open the SSH connections minds needs: whether any of the "
            "endpoints it dials served a banner, or failing that any of the public quorum hosts "
            "on port 22. UNKNOWN while the internet facet is down, which leaves port 22 untested."
        )
    )
    observed_at: datetime | None = Field(description="When this reading was taken; None if none has been taken")

    @property
    def environment_block(self) -> EnvironmentBlock:
        """The device-level condition this reading establishes, if any.

        Only a *confirmed* facet blocks. ``UNKNOWN`` yields ``NONE``, so a
        reading that has not been taken -- or one a wake invalidated -- can never
        withhold an action or explain away a failure.

        SSH is only ever reported as blocked while the internet facet is
        confirmed up. With the internet down, every SSH endpoint failing says
        nothing about port 22 in particular, and the user has one problem rather
        than two.
        """
        if self.internet is ConnectivityFacet.OFFLINE:
            return EnvironmentBlock.OFFLINE
        if self.internet is ConnectivityFacet.ONLINE and self.ssh is ConnectivityFacet.OFFLINE:
            return EnvironmentBlock.SSH_BLOCKED
        return EnvironmentBlock.NONE

    @property
    def environment_condition(self) -> EnvironmentCondition:
        """The device-level condition as a surface should describe it.

        The same answer as :attr:`environment_block` for a reading that has
        been taken, and ``UNKNOWN`` for one that has not -- none yet, or one a
        wake invalidated. The internet facet alone decides that: the SSH facet
        is left ``UNKNOWN`` by design whenever the internet is down, and that is
        a measured reading, not a missing one.
        """
        if self.internet is ConnectivityFacet.UNKNOWN:
            return EnvironmentCondition.UNKNOWN
        return EnvironmentCondition(self.environment_block.value)


_UNKNOWN_READING: Final[ConnectivityReading] = ConnectivityReading(
    internet=ConnectivityFacet.UNKNOWN, ssh=ConnectivityFacet.UNKNOWN, observed_at=None
)


class NetworkProber(MutableModel, ABC):
    """The two network questions the detector asks, isolated so tests can answer them.

    Both must report a failure rather than raise: an unreachable host is the
    answer, not an error, and the detector has no better handling for a socket
    error than "that endpoint did not answer".
    """

    @abstractmethod
    def is_reachable(self, host: str, port: int) -> bool:
        """Whether a TCP connection to ``host:port`` can be established (name resolution included)."""

    @abstractmethod
    def is_ssh_server(self, host: str, port: int) -> bool:
        """Whether ``host:port`` answers with an SSH protocol banner."""


def _address_attempt_seconds(remaining_seconds: float, remaining_address_count: int) -> float:
    """How long one address may take: an equal share of what is left of its endpoint's budget.

    An address that drops the SYN rather than refusing it takes every second it
    is given, and a routable IPv6 address on a network that blackholes IPv6
    egress is exactly that -- the case the walk over an endpoint's addresses
    exists for. Handed more than its share, it would report an endpoint that
    answers on a later address as unreachable, which for a dual-stack quorum
    host reads as this device being offline while its IPv4 works.

    An equal share rather than a fixed reservation, because what has to be held
    back is every attempt still to come, not one of them: ``bitbucket.org``
    publishes three AAAA and three A records, so a rule that reserved for one
    successor would let the second silent address spend what the other four
    needed. Dividing by the addresses left instead leaves each of them something
    to dial on, and the last -- which is the whole of what remains, since its
    count is one -- is reached whatever the ones before it did.

    What that costs is a narrower window per address on a many-addressed
    endpoint. It is the same trade ``_DEFAULT_PROBE_TIMEOUT_SECONDS`` already
    makes: a network that cannot answer a well-connected host inside a share of
    that budget is not one a workspace is reachable over either.
    """
    return remaining_seconds / remaining_address_count


class SocketNetworkProber(NetworkProber):
    """A :class:`NetworkProber` backed by blocking stdlib sockets.

    Deliberately below the HTTP layer: an HTTP client's own retries, redirects,
    proxy handling and TLS would each turn a clean "did not answer" into
    something else, and the SSH facet has no HTTP equivalent at all.
    """

    timeout_seconds: float = Field(
        default=_DEFAULT_PROBE_TIMEOUT_SECONDS,
        description="Budget for connecting to one endpoint, however many addresses it resolves to, and for the banner read.",
    )

    def _connect_within_budget(self, host: str, port: int) -> socket.socket | None:
        """Connect to ``host:port``, spending one budget on the endpoint rather than one per address.

        ``socket.create_connection`` cannot be asked for that: it walks the
        resolved addresses itself and re-applies its ``timeout`` to each one, so
        a single multi-homed host spends the budget once per address --
        ``bitbucket.org`` answers on three A records, measured at 3.0s against a
        1.5s budget where this loop takes 1.5s. That multiplication is what put
        the whole probe's worst case above the concurrency group's exit budget,
        and it is what a quit landing mid-round has to wait out.

        The addresses are still tried in turn, because that is what makes a host
        reachable at all when its first address family is not routable from
        here. What changes is that they share one deadline, and that each takes
        an equal share of what is left of it (:func:`_address_attempt_seconds`)
        -- otherwise one that blackholes spends the lot and the walk is a walk
        in name only.

        Returns the connected socket, which the caller owns, or None when no
        address answered -- refused, unreachable, unopenable, or still silent
        when its share of the budget ran out. Raises what resolution raises; the
        callers report that as the endpoint not answering.

        The budget starts once the addresses are in hand. Charging resolution to
        it would bound nothing -- ``getaddrinfo`` blocks for as long as it blocks
        either way -- while a lookup slower than the budget would leave nothing
        to connect with, reporting a reachable endpoint as unreachable. An
        uncached lookup right after a reassociation is exactly when this runs.
        """
        address_infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        deadline = time.monotonic() + self.timeout_seconds
        for index, (family, socket_type, protocol, _canonical_name, address) in enumerate(address_infos):
            attempt_seconds = _address_attempt_seconds(
                remaining_seconds=deadline - time.monotonic(),
                remaining_address_count=len(address_infos) - index,
            )
            # Zero would put the socket in non-blocking mode rather than time
            # out, so the budget is spent as soon as it is not positive.
            if attempt_seconds <= 0.0:
                logger.debug("Probe of {}:{} spent its budget before reaching {}", host, port, address[0])
                break
            try:
                connection = socket.socket(family, socket_type, protocol)
            # A family the kernel does not support is one more address that
            # cannot be reached from here, so it falls through to the next one
            # rather than ending the walk.
            except OSError as e:
                logger.debug("Probe of {}:{} could not open a socket for {}: {}", host, port, address[0], e)
                continue
            try:
                connection.settimeout(attempt_seconds)
                connection.connect(address)
            except OSError as e:
                logger.debug("Probe of {}:{} did not connect to {}: {}", host, port, address[0], e)
                connection.close()
                continue
            return connection
        return None

    # UnicodeError alongside OSError in both of these: the endpoints come from
    # discovery, and a hostname getaddrinfo's idna codec refuses to encode -- a
    # label past 63 characters -- raises a ValueError rather than an OSError. A
    # non-ASCII label is not one of these: it punycodes and comes back as an
    # ordinary gaierror. That is still this endpoint not answering, and letting
    # it out would take the detector's loop thread with it -- leaving the watch
    # latched on with nothing left to lift it.
    def is_reachable(self, host: str, port: int) -> bool:
        try:
            connection = self._connect_within_budget(host, port)
        except (OSError, UnicodeError) as e:
            logger.debug("Connectivity probe of {}:{} did not connect: {}", host, port, e)
            return False
        if connection is None:
            return False
        with connection:
            return True

    def is_ssh_server(self, host: str, port: int) -> bool:
        try:
            connection = self._connect_within_budget(host, port)
        except (OSError, UnicodeError) as e:
            logger.debug("SSH probe of {}:{} did not connect: {}", host, port, e)
            return False
        if connection is None:
            return False
        try:
            with connection:
                # Read until the prefix is in hand or the peer stops sending. A
                # single recv may return fewer bytes than asked for, and taking
                # that short read as "not an SSH server" would report a working
                # endpoint as blocked -- a verdict the user is shown.
                #
                # One budget for the whole read rather than one per recv: the
                # prefix is seven bytes, so a peer dribbling one byte per timeout
                # would otherwise hold this endpoint -- and the detector's probe
                # lock behind it -- for seven times the budget. A filtering
                # middlebox, which is exactly what this facet exists to catch, is
                # the likeliest thing to trickle.
                read_deadline = time.monotonic() + self.timeout_seconds
                banner = b""
                while len(banner) < len(_SSH_BANNER_PREFIX):
                    remaining_seconds = read_deadline - time.monotonic()
                    # Zero would put the socket in non-blocking mode rather than
                    # time out, so the budget is spent as soon as it is not
                    # positive.
                    if remaining_seconds <= 0.0:
                        break
                    connection.settimeout(remaining_seconds)
                    chunk = connection.recv(len(_SSH_BANNER_PREFIX) - len(banner))
                    if not chunk:
                        break
                    banner += chunk
        except OSError as e:
            logger.debug("SSH probe of {}:{} did not answer: {}", host, port, e)
            return False
        return banner.startswith(_SSH_BANNER_PREFIX)


# No-arg, because the device's condition is a single app-wide fact: consumers
# re-read the detector (or their own owed work) rather than being handed it.
OnConnectivityRecoveryCallback = Callable[[], None]
OnConnectivityChangeCallback = Callable[[], None]
# Either kind, for the places that carry a mixed batch to the fence: a reading
# that both recovers and moves the condition owes both, and a wake that blanks a
# bad reading owes only the change ones.
ConnectivityCallback = OnConnectivityRecoveryCallback | OnConnectivityChangeCallback


def _fire_connectivity_callbacks(callbacks: list[ConnectivityCallback]) -> None:
    """Run detector callbacks, keeping one failure from swallowing the rest.

    Always called with no detector lock held: these dispatch restarts and take
    other components' locks, and one of them re-entering a probe would deadlock
    on a lock that is not reentrant.

    The families are the widest of what either kind can reach the fence with. A
    recovery callback dispatches restarts, and a restart's registry and tracker calls
    raise ``MindError`` (a ``ClickException``, so not a ``RuntimeError``) and
    ``MngrError`` -- which, escaping, would kill the loop this is called from and
    with it the only thing that can ever observe the network coming back:
    ``_is_watching`` would stay on with nothing left to turn it off, and every
    owed restart would be stranded for the life of the process.
    """
    for callback in callbacks:
        try:
            callback()
        except (MindError, MngrError, OSError, RuntimeError, ValueError) as e:
            logger.opt(exception=e).warning("ConnectivityDetector callback failed: {}", e)


class ConnectivityDetector(MutableModel):
    """Whether this device can reach anything, measured only when it is about to matter.

    Construct one per minds process. A consumer that is about to act on the
    answer calls :meth:`probe_now` and blocks for it (seconds, on a worker
    thread of its own -- never on a loop that other work is waiting behind); a
    consumer that only wants to render the last known state calls
    :meth:`get_reading`, which never touches the network.

    A bad reading is what turns the background loop on: while one is
    outstanding, :meth:`run_background_loop` re-probes every few seconds so the
    recovery is noticed promptly and the callbacks registered through
    :meth:`add_on_recovery_callback` can fire whatever was owed. A good reading
    turns it back off. There is no other background probing -- a machine sitting
    idle on a working network generates no traffic from here.

    A wake invalidates the cache (:meth:`invalidate_after_wake`) rather than
    re-probing: whatever was true before the lid closed says nothing about the
    network the laptop woke up on, and an unknown reading is the honest state
    until something asks.
    """

    prober: NetworkProber = Field(
        default_factory=SocketNetworkProber, description="How the individual endpoint checks are performed."
    )
    probe_hosts: tuple[str, ...] = Field(
        default=_PROBE_HOSTS, description="Independent public hosts forming the quorum for both facets."
    )
    workspace_ssh_endpoints_fn: Callable[[], tuple[SshEndpoint, ...]] = Field(
        default=lambda: (),
        description=(
            "The SSH endpoints minds actually dials, sampled fresh at probe time. Supplied as a "
            "callable so this module stays a leaf: the endpoints come from discovery, which knows "
            "them per machine. Empty leaves the SSH facet on the public quorum alone. Order "
            "matters: only the first few are measured, so the ones most likely to answer come first."
        ),
    )
    poll_interval_seconds: float = Field(
        default=_DEFAULT_CONNECTIVITY_POLL_INTERVAL_SECONDS,
        description="How often the background loop re-probes while a bad reading is outstanding.",
    )
    now_fn: Callable[[], datetime] = Field(
        default=_utc_now, description="Injectable UTC clock (overridden in tests for deterministic timestamps)."
    )
    shutdown_event: SkipValidation[ReadOnlyEvent] | None = Field(
        default=None,
        description=(
            "Set when the app is going down, so a probe stops opening connections instead of "
            "holding the concurrency group's drain for a round of timeouts. None never interrupts."
        ),
    )
    concurrency_group: SkipValidation[ConcurrencyGroup] = Field(
        description="Parent group both of the probe's concurrent rounds fan out under."
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Serializes the probe itself, which is seconds long, so a consumer's
    # blocking call and the background loop cannot run overlapping probes.
    _probe_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _reading: ConnectivityReading = PrivateAttr(default=_UNKNOWN_READING)
    # Bumped by every wake. A probe carries the generation it started in, and a
    # reading from an older one is dropped rather than stored: the connections
    # it made were made before the machine stopped running, so it describes a
    # network that may no longer be in front of the laptop -- and its
    # ``observed_at`` says "now", which would make it look fresh enough to reuse
    # and, if it happened to be good, would fire the recovery edge and drain
    # every owed restart onto whatever network the laptop actually woke up on.
    _reading_generation: int = PrivateAttr(default=0)
    # Whether a bad reading is outstanding. Distinct from the reading itself
    # because a wake blanks the reading to UNKNOWN without settling anything:
    # the loop must keep watching until a probe actually reports the network
    # back, or the owed work would be stranded.
    _is_watching: bool = PrivateAttr(default=False)
    _on_recovery_callbacks: list[OnConnectivityRecoveryCallback] = PrivateAttr(default_factory=list)
    _on_change_callbacks: list[OnConnectivityChangeCallback] = PrivateAttr(default_factory=list)

    # -- Public callback registration -------------------------------------

    def add_on_recovery_callback(self, callback: OnConnectivityRecoveryCallback) -> None:
        """Register a no-arg callback fired when a probe finds the device reachable again.

        Fires only on the bad -> good edge, and only for a probe that observed
        the recovery -- never for the cache being invalidated. Callbacks run on
        whichever thread ran that probe, outside this detector's locks, so they
        may take other components' locks and dispatch work.
        """
        with self._lock:
            self._on_recovery_callbacks.append(callback)

    def add_on_change_callback(self, callback: OnConnectivityChangeCallback) -> None:
        """Register a no-arg callback fired whenever the device's condition changes.

        Broader than :meth:`add_on_recovery_callback`, which is only the edge
        back to reachable: this fires in both directions, because a surface that
        renders the condition has to raise it as well as drop it. Callbacks run
        outside this detector's locks, on whichever thread moved the condition:
        the one that took the probe, or -- when a wake blanks a bad reading --
        the heartbeat thread.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    # -- Readings ---------------------------------------------------------

    def get_reading(self) -> ConnectivityReading:
        """The last reading taken, without touching the network.

        For surfaces that render the current state (and for a failure being
        explained after the fact). A caller whose decision depends on the answer
        wants :meth:`probe_now` instead -- this one can be arbitrarily stale, and
        is ``UNKNOWN`` until the first probe lands.
        """
        with self._lock:
            return self._reading

    def is_watching_for_recovery(self) -> bool:
        """Whether a bad reading is outstanding, so a recovery edge is still to come.

        For a consumer that owes work to that edge and needs to know whether one
        is coming at all. False means the detector has stopped probing: either it
        never found anything wrong, or the recovery it was watching for has
        already fired -- and it fires only on the bad -> good transition, so a
        caller that missed it will not be given another.

        Deliberately not the same question as ``get_reading()`` answering NONE. A
        wake blanks the reading to UNKNOWN without settling anything, and this
        stays True across it: the network the laptop woke up on has not been
        looked at, and the owed work is still owed.
        """
        with self._lock:
            return self._is_watching

    def probe_now(self, max_reuse_age_seconds: float = 0.0) -> ConnectivityReading:
        """Probe both facets and return the fresh reading. Blocks for seconds.

        The internet facet is the quorum's disjunction: one host answering on
        443 is enough, since the question is whether this device can reach
        *anything*. The SSH facet is only asked once that has been established,
        and is measured against the endpoints minds itself dials -- see
        :meth:`_read_ssh_facet`.

        ``max_reuse_age_seconds`` lets a caller accept a reading that recent
        instead of taking one of its own. It exists for the case this whole
        detector is for: a dropped network takes every remote workspace down on
        the same probe tick, so their gates all arrive here at once, and without
        it each would queue behind the last and re-measure the same network --
        making the last machine's verdict tens of seconds later than the first's
        for no new information. Zero, the default, always probes; the background
        loop relies on that to observe a recovery.

        What comes back is the reading that is *in force*, which is not always
        the one just measured: a wake landing mid-probe disqualifies the whole
        measurement, and the caller is handed the blanked ``UNKNOWN`` instead of
        a description of the network the laptop went to sleep on. Nothing else
        would be safe to act on -- see :meth:`_store_reading`.
        """
        with self._probe_lock:
            if max_reuse_age_seconds > 0.0:
                reading = self.get_reading()
                if (
                    reading.observed_at is not None
                    and (self.now_fn() - reading.observed_at).total_seconds() <= max_reuse_age_seconds
                ):
                    return reading
            # Taken before the first connection, so a wake landing at any point
            # during the measurement disqualifies the whole of it.
            with self._lock:
                generation = self._reading_generation
            # Checked before the round as well as after it, the way
            # :meth:`_read_ssh_facet` checks before each of its own: the round
            # starts threads on the parent group, which refuses outright once
            # that group is shutting down rather than answering short.
            if self._is_shutting_down():
                return self._abandon_probe()
            is_internet_up = self._does_any_probe_host_answer(_HTTPS_PORT)
            if self._is_shutting_down():
                return self._abandon_probe()
            if not is_internet_up:
                # With nothing reachable at all, port 22 has not been tested --
                # every SSH attempt would fail for the reason already found.
                reading = ConnectivityReading(
                    internet=ConnectivityFacet.OFFLINE,
                    ssh=ConnectivityFacet.UNKNOWN,
                    observed_at=self.now_fn(),
                )
            else:
                ssh_facet = self._read_ssh_facet()
                if self._is_shutting_down():
                    return self._abandon_probe()
                reading = ConnectivityReading(
                    internet=ConnectivityFacet.ONLINE,
                    ssh=ssh_facet,
                    observed_at=self.now_fn(),
                )
            reading, owed_callbacks = self._store_reading(reading, generation)
        # Fired after the probe lock is released, which is what makes the
        # callback contract keepable: a recovery callback dispatches the
        # restarts that were owed, taking other components' locks and spawning
        # threads, and every other gate waiting to read this same network would
        # otherwise be queued behind all of it.
        _fire_connectivity_callbacks(owed_callbacks)
        return reading

    def _is_shutting_down(self) -> bool:
        """Whether the app is going down, and this probe should stop opening connections."""
        return self.shutdown_event is not None and self.shutdown_event.is_set()

    def _abandon_probe(self) -> ConnectivityReading:
        """Answer with the reading already in force, having measured nothing.

        A round the shutdown cut short read "nothing answered" for the endpoints
        it never got to, so storing it would record a dead network on the way
        out -- and fire the change callbacks that render one. The caller gets
        the same thing a wake-disqualified probe hands back.
        """
        logger.debug("Abandoning a connectivity probe: the app is shutting down")
        return self.get_reading()

    def _ask_probe_host(self, host: str, port: int) -> bool:
        """Whether one quorum host answers on ``port``, opening no connection once shut down."""
        if self._is_shutting_down():
            return False
        return self.prober.is_reachable(host, port)

    def _does_any_probe_host_answer(self, port: int) -> bool:
        """Whether any public quorum host answers on ``port``, asking them all at once.

        Concurrent for the same reason the SSH round is (see
        :meth:`_does_any_ssh_endpoint_answer`) and with the same trade: "one of
        them answered" could stop at the first, but "none of them did" -- the
        reading that declares this device offline -- has to hear from every
        host, and asking them one at a time makes that the sum of every budget
        rather than the slowest one. It was the largest single term in the
        probe's worst case, which has to fit inside the concurrency group's exit
        budget.

        What it costs is connections on the path where the answer is good and
        nothing is waiting on it: three rather than one, per probe.

        A short round on shutdown reads as "nothing answered", which is why
        every caller re-checks :meth:`_is_shutting_down` before believing it.
        """
        if not self.probe_hosts:
            return False
        with ConcurrencyGroupExecutor(
            parent_cg=self.concurrency_group,
            name="connectivity-internet-probe",
            max_workers=len(self.probe_hosts),
        ) as executor:
            futures = [executor.submit(self._ask_probe_host, host, port) for host in self.probe_hosts]
            # Resolved into a list first, for the reason the SSH round gives.
            return any([future.result() for future in futures])

    def _ask_ssh_endpoint(self, endpoint: SshEndpoint) -> bool:
        """Whether one endpoint serves an SSH banner, opening no connection once shut down.

        The shutdown is re-checked here rather than only around the round: this
        runs once per endpoint, concurrently, so it is the last point at which a
        quit can still stop a connection from being opened.
        """
        if self._is_shutting_down():
            return False
        return self.prober.is_ssh_server(endpoint.host, endpoint.port)

    def _does_any_ssh_endpoint_answer(self, endpoints: tuple[SshEndpoint, ...]) -> bool:
        """Whether any of ``endpoints`` serves an SSH banner, asking them all at once.

        Concurrent because of which answer costs the most. "One of them answered"
        can stop at the first, but "none of them did" -- the verdict that
        withholds a dispatch -- has to hear from every endpoint, and asking them
        one at a time makes that the sum of every budget. A round of one
        workspace endpoint and the three public hosts was measured at 9.25s
        against a 1.5s budget that was then per *connection*, so one multi-homed
        public host spent it once per resolved address; the budget is the
        endpoint's now (:meth:`SocketNetworkProber._connect_within_budget`).

        The trade is that every endpoint is now asked even when the first would
        have settled it. The wall clock is the slowest single endpoint either
        way; what it costs is connections, on the path where the answer is good
        and nothing is waiting on it.

        Each endpoint re-checks the shutdown before dialling (see
        :meth:`_ask_ssh_endpoint`), so a quit landing mid-round still opens no
        connection it has not already opened. A round abandoned that way reads as
        "nothing answered", which is why every caller re-checks
        :meth:`_is_shutting_down` before believing it.
        """
        if not endpoints:
            return False
        with ConcurrencyGroupExecutor(
            parent_cg=self.concurrency_group,
            name="connectivity-ssh-probe",
            max_workers=len(endpoints),
        ) as executor:
            futures = [executor.submit(self._ask_ssh_endpoint, endpoint) for endpoint in endpoints]
            # Resolved into a list first: `any` over a generator would return on
            # the first True and leave the rest of the round unread, and the
            # executor's own drain would wait for them anyway.
            return any([future.result() for future in futures])

    def _read_ssh_facet(self) -> ConnectivityFacet:
        """Whether this device can open the SSH connections minds needs. Assumes the internet is up.

        Asks minds' *own* endpoints first, because they are the only ones whose
        answer is the question: a machine's host is reached on whatever port its
        provider forwarded, and for imbue_cloud that is a box-forwarded port in
        the 22000-32000 range rather than 22. One of them answering settles it --
        the network passes what minds needs, whatever it does to port 22.

        When none of them answer, the public quorum breaks the tie that the
        endpoints alone cannot. Every one of minds' machines failing has two
        explanations, and only one of them is the network: if a public host
        still serves an SSH banner, SSH leaves this device fine and those
        machines are simply unreachable for reasons of their own -- so the facet
        stays ONLINE and the recovery paths go on treating it as a machine
        problem, which is what it is. Only when the public quorum fails too is
        this network blocking SSH.

        With no endpoints known the quorum answers alone, which is all there is
        to go on. It still speaks either way: the facet feeds the app-level
        notice, so a network that blocks :22 is named on a device that may have
        nothing on the far side of it. What it gates depends on why the
        endpoints are missing. With no remote machines there is no dispatch to
        withhold. With discovery not yet reporting there can be -- a machine
        whose coordinate discovery has not supplied is probed as failing and can
        reach STUCK, and counts as network-dependent -- so its start is withheld
        on a quorum-only verdict. That is the conservative direction this module
        takes everywhere: it costs a wait that the network returning ends,
        against dispatching over a network that cannot carry it.
        """
        endpoints = tuple(dict.fromkeys(self.workspace_ssh_endpoints_fn()))[:_MAX_SAMPLED_WORKSPACE_SSH_ENDPOINTS]
        if self._is_shutting_down():
            return ConnectivityFacet.OFFLINE
        if self._does_any_ssh_endpoint_answer(endpoints):
            return ConnectivityFacet.ONLINE
        if self._is_shutting_down():
            return ConnectivityFacet.OFFLINE
        is_public_ssh_up = self._does_any_ssh_endpoint_answer(
            tuple(SshEndpoint(host=host, port=_PUBLIC_SSH_PORT) for host in self.probe_hosts)
        )
        if is_public_ssh_up:
            if endpoints:
                logger.info(
                    "None of {} of this device's own SSH endpoints answered, but public SSH does: "
                    "treating the machines as unreachable rather than the network as blocking SSH",
                    len(endpoints),
                )
            return ConnectivityFacet.ONLINE
        return ConnectivityFacet.OFFLINE

    def invalidate_after_wake(self, wake_at: datetime) -> None:
        """Drop the cached reading to ``UNKNOWN`` (registered as the sleep tracker's wake callback).

        The laptop may have woken somewhere else entirely, so the last reading
        describes a network that is no longer in front of it. Blanking rather
        than re-probing keeps this off the wake path: an unknown reading blocks
        nothing, and the next consumer that actually needs an answer will take
        one. Anything the detector was already watching stays watched.

        A probe that is already in flight is blanked too, by moving the
        generation its result will be checked against: it was measuring the
        network on the far side of the sleep, and taking no lock it could hold
        is what keeps this off the wake path at all.
        """
        with self._lock:
            previous = self._reading
            self._reading = _UNKNOWN_READING
            self._reading_generation += 1
            # Blanking a bad reading drops the condition the surfaces are
            # rendering, so they have to hear about it like any other change.
            callbacks = (
                list(self._on_change_callbacks) if previous.environment_block is not EnvironmentBlock.NONE else []
            )
        if previous.observed_at is not None:
            logger.info("Connectivity reading invalidated by the wake at {}", wake_at.isoformat())
        _fire_connectivity_callbacks(callbacks)

    # -- Background loop --------------------------------------------------

    def run_background_loop(self, concurrency_group: ConcurrencyGroup) -> None:
        """Re-probe while a bad reading is outstanding; otherwise do nothing at all.

        The loop ticks unconditionally but only *probes* while watching, so a
        device on a working network never generates traffic from here. Started
        by ``minds run``; the thread lives for the process.

        The probe is fenced for the same reason the callbacks are, and it is the
        more valuable of the two: this loop is the only thing that can ever
        observe the network coming back, so an escape here would leave
        ``_is_watching`` on with nothing left to turn it off -- every owed
        restart stranded, every held refresh unpublished, and the surfaces
        latched on the last bad reading. The families are the widest the probe
        can reach here with: ``workspace_ssh_endpoints_fn`` is a walk over
        discovery, and each round starts threads on the parent group, which
        refuses once that group is shutting down or has a failed strand.
        """
        while not concurrency_group.is_shutting_down():
            with self._lock:
                is_watching = self._is_watching
            if is_watching:
                try:
                    self.probe_now()
                except (
                    MindError,
                    MngrError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    ConcurrencyGroupError,
                    ConcurrencyExceptionGroup,
                ) as e:
                    logger.opt(exception=e).warning(
                        "Connectivity probe failed; the device's condition stays as it was: {}", e
                    )
            # Sleep on the group's shutdown event (not a throwaway Event) so the
            # loop wakes immediately when shutdown is triggered instead of
            # holding the concurrency-group exit for up to a full interval.
            concurrency_group.shutdown_event.wait(timeout=self.poll_interval_seconds)

    # -- Internals --------------------------------------------------------

    def _store_reading(
        self, reading: ConnectivityReading, generation: int
    ) -> tuple[ConnectivityReading, list[ConnectivityCallback]]:
        """Adopt a fresh reading; return the one now in force and the callbacks it owes.

        Returns rather than fires, so the caller can run them once it has let go
        of the probe lock -- see :meth:`probe_now`. The recovery callbacks are
        owed only on the bad -> good edge; the change callbacks are owed
        whenever the condition itself moved, in either direction, since a
        surface that renders it has to raise it as well as drop it.

        A reading whose probe began in an older generation is dropped instead: a
        wake landed while it was being taken, so it measured the network the
        laptop went to sleep on. Nothing is owed for it, least of all a recovery.

        The reading is returned rather than left to the caller's own copy for the
        same reason it is dropped here. The prober's answer is not evidence about
        the network in front of the laptop now, and a caller acting on it is
        acting on the far side of a sleep: a stale OFFLINE has the gate withhold
        a start and record it as owed against a detector that is not watching for
        anything and so will never release it, and a stale ONLINE dispatches over
        a network nothing has looked at. What is handed back instead is the
        blanked ``UNKNOWN``, which suppresses nothing -- the state the module
        treats as "no evidence" everywhere else.
        """
        block = reading.environment_block
        with self._lock:
            if generation != self._reading_generation:
                logger.info(
                    "Dropping a connectivity reading (internet={} ssh={}) taken across a wake",
                    reading.internet.value,
                    reading.ssh.value,
                )
                return self._reading, []
            previous = self._reading
            was_watching = self._is_watching
            self._reading = reading
            self._is_watching = block is not EnvironmentBlock.NONE
            callbacks: list[ConnectivityCallback] = (
                list(self._on_recovery_callbacks) if was_watching and not self._is_watching else []
            )
            if previous.environment_block is not block:
                callbacks = callbacks + list(self._on_change_callbacks)
        if (previous.internet, previous.ssh) != (reading.internet, reading.ssh):
            logger.info(
                "Connectivity now reads internet={} ssh={} (device condition: {})",
                reading.internet.value,
                reading.ssh.value,
                block.value,
            )
        return reading, callbacks
