"""The wrapper Imbue Studio puts around the ``mngr forward`` plugin subprocess.

Phase 2 deletes Imbue Studio's in-process subdomain-forwarding, auth, and observe-
spawning code; this file replaces them with a thin consumer that:

- spawns ``mngr forward --observe-via-file`` as a subprocess so it tails the
  shared discovery events file written by the single ``mngr observe`` under
  ``mngr latchkey forward`` instead of running its own observe;
- reads stdout line-by-line on a background thread and parses each line as a
  ``ForwardEnvelope``;
- dispatches by ``stream``: ``observe`` lines are folded into a shared
  ``DiscoveryStateAggregator`` (the span-aware, per-provider reconciler), whose
  view is then pushed into the surviving ``MngrCliBackendResolver`` and fanned
  out to a set of ``on_agent_discovered`` / ``on_agent_destroyed`` callbacks;
  ``event`` lines drive the resolver's
  service map and fan out to request callbacks; ``forward`` lines
  feed the ``system_interface_backend_failure`` health tracker and the
  ``listening`` port handshake;
- watches the subprocess for premature exit and reports it (the consumer is
  dead, so the discovery pipeline is down) to the discovery-health watchdog
  via registered ``on_unexpected_exit`` callbacks.

Provider-set changes are picked up by bouncing the detached ``mngr latchkey
forward`` supervisor (the single discovery observer); the tailer here then sees
the fresh snapshot automatically, so this consumer no longer sends ``SIGHUP``.
"""

import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import AbstractSet
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import SERVICES_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.backend_resolver import ServiceDeregisteredRecord
from imbue.minds.desktop_client.backend_resolver import ServiceShareKind
from imbue.minds.desktop_client.backend_resolver import parse_service_log_record
from imbue.minds.desktop_client.environment_signals import SleepTracker
from imbue.minds.errors import EnvelopeStreamConsumerError
from imbue.minds.utils.secret_redaction import REDACTED_PLACEHOLDER
from imbue.minds.utils.secret_redaction import redact_secret_flag_values
from imbue.minds.utils.secret_redaction import redact_url_credentials_in_text
from imbue.mngr.api.discovery_aggregator import AggregatorDelta
from imbue.mngr.api.discovery_aggregator import DiscoveryStateAggregator
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import DiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoverySchemaMismatchWarner
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import make_provider_discovery_snapshot_event
from imbue.mngr.api.discovery_log_suppression import DiscoveryErrorLogSuppressor
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_DEFAULT_MNGR_HOST_DIR: Final[Path] = Path.home() / ".mngr"
_PREAUTH_TOKEN_LENGTH: Final[int] = 64

OnAgentDiscoveredCallback = Callable[[AgentId, RemoteSSHInfo | None, str], None]
OnAgentDestroyedCallback = Callable[[AgentId], None]
OnSystemInterfaceBackendFailureCallback = Callable[
    [AgentId, SystemInterfaceBackendFailureReason, int | None, str | None], None
]
OnSystemInterfaceBackendAnsweredCallback = Callable[[AgentId, int | None], None]
OnUnexpectedExitCallback = Callable[[int], None]


def _parse_backend_failure_reason(raw_reason: str) -> SystemInterfaceBackendFailureReason:
    """Map an envelope's ``reason`` string to the enum, falling back to ``CONNECT_ERROR``.

    A reason this build does not know is still a report that the plugin could
    not reach a backend, so it is read as the generic connection-class failure
    rather than dropped. Producer and consumer ship pinned to the same commit,
    so this should never fire -- it exists so that if the pinning ever slips,
    the cost is a coarser verdict rather than a workspace whose outage Imbue Studio
    never hears about at all.
    """
    try:
        return SystemInterfaceBackendFailureReason(raw_reason)
    except ValueError:
        logger.warning(
            "Unknown system_interface_backend_failure reason {!r}; treating it as a connection failure", raw_reason
        )
        return SystemInterfaceBackendFailureReason.CONNECT_ERROR


def _parse_status_code(payload: dict[str, Any]) -> int | None:
    """Read an envelope payload's optional ``status_code``, reading anything unparseable as absent.

    Both payloads that carry one send it as null for the case that has no
    status (a websocket the backend accepted; a failure that never got a
    response), so there is nothing a garbled value could be reported as that
    "no status" does not already cover.
    """
    raw_status_code = payload.get("status_code")
    if raw_status_code is None:
        return None
    try:
        return int(raw_status_code)
    except (ValueError, TypeError):
        logger.warning("Unparseable status_code {!r} on a forward envelope; reading it as no status", raw_status_code)
        return None


class ForwardSubprocessConfig(FrozenModel):
    """Args for the ``mngr forward`` subprocess that ``minds run`` spawns.

    Note: the preauth cookie is *not* a configurable field. It is freshly
    generated inside ``start_mngr_forward`` (so each run has a fresh
    secret) and returned to the caller as the second element of the
    tuple. Callers hand it to the Electron shell, which pre-sets
    ``mngr_forward_session=<value>`` on ``localhost:<port>``.
    """

    service: str = Field(default="system_interface", description="Service name to forward")
    agent_include: tuple[str, ...] = Field(
        default=("has(agent.labels.is_primary)",),
        description="CEL include filters passed to --agent-include",
    )
    reverse_specs: tuple[str, ...] = Field(
        default=(),
        description="--reverse REMOTE:LOCAL pairs to set up",
    )
    embedder_origins: tuple[str, ...] = Field(
        default=(),
        description="Origins allowed to embed workspace content, passed to --embedder-origin",
    )
    mngr_binary: str = Field(default=MNGR_BINARY, description="Path to mngr binary")
    mngr_host_dir: Path = Field(default=_DEFAULT_MNGR_HOST_DIR, description="MNGR_HOST_DIR for the subprocess")
    request_headers_file: Path | None = Field(
        default=None,
        description=(
            "The file passed as --request-headers-file: the headers the proxy stamps on every forwarded "
            "request per agent (the desktop writes its X-Imbue-Identity contract there)"
        ),
    )


class _DroppedPreStartErrorTally(FrozenModel):
    """The pre-start snapshots dropped for one provider that all carried one same error."""

    error: DiscoveryError = Field(description="The error every tallied snapshot carried")
    count: int = Field(description="How many snapshots carrying it have been dropped so far")
    last_snapshot_at: datetime = Field(description="``discovery_finished_at`` of the most recent one")


class _PreStartErrorDropLogger(MutableModel):
    """Collapses the dropped pre-start provider errors into one line per distinct error.

    The events-file backlog holds one snapshot per discovery cycle for however
    long Imbue Studio was closed, and a wedged provider errors on every one of them, so
    logging each drop scales with the downtime. Route every dropped error
    through :meth:`record_dropped_error`, which only tallies, and end each
    provider's replay with :meth:`flush_provider`, which logs one counted line
    per distinct error the backlog dropped for it.

    Nothing is logged while the tally builds, because a provider that is still
    broken re-asserts its error on the first fresh cycle (which the shared
    error-log suppressor reports); these lines are the historical record of the
    gap, so they are worth exactly one line apiece.

    Tallies are kept per distinct error rather than for the latest one alone,
    because a wedged provider need not repeat a single error: a discovery that
    overruns its timeout yields a timeout snapshot and then, once the abandoned
    read resolves, a snapshot carrying the underlying failure, so its backlog
    alternates two errors. A provider whose backlog errors flap (network down,
    briefly up, down again) likewise builds one tally per distinct error rather
    than one per uninterrupted run of it -- only a *post-start* snapshot ends
    the replay, so a clean pre-start one does not split the tally.

    Thread-safe; use one instance per consumer.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Keyed by ``(type_name, message)`` rather than by the whole ``DiscoveryError``,
    # matching the shared ``DiscoveryErrorLogSuppressor``: an error also carries a
    # captured traceback, and folding that into the key would split one wedged
    # provider's tally the moment its traceback varied -- restoring exactly the
    # line-per-cycle flood this collapser exists to prevent, in lines that read
    # identically (only the message is printed).
    _tally_by_error_by_provider_name: dict[ProviderInstanceName, dict[tuple[str, str], _DroppedPreStartErrorTally]] = (
        PrivateAttr(default_factory=dict)
    )

    def record_dropped_error(
        self, provider_name: ProviderInstanceName, error: DiscoveryError, snapshot_at: datetime
    ) -> None:
        """Tally one dropped pre-start error, to be logged when the provider's replay ends."""
        error_key = (error.type_name, error.message)
        with self._lock:
            tally_by_error = self._tally_by_error_by_provider_name.setdefault(provider_name, {})
            previous = tally_by_error.get(error_key)
            if previous is None:
                tally_by_error[error_key] = _DroppedPreStartErrorTally(
                    error=error, count=1, last_snapshot_at=snapshot_at
                )
            else:
                tally_by_error[error_key] = previous.model_copy_update(
                    to_update(previous.field_ref().count, previous.count + 1),
                    to_update(previous.field_ref().last_snapshot_at, snapshot_at),
                )

    def flush_provider(self, provider_name: ProviderInstanceName) -> None:
        """Log what one provider's replay dropped: one counted line per distinct error, in first-seen order."""
        with self._lock:
            tally_by_error = self._tally_by_error_by_provider_name.pop(provider_name, None)
        for tally in (tally_by_error or {}).values():
            logger.info(
                "Dropped pre-start provider errors for {} while replaying the events-file backlog "
                "(last at {}): {}x {}",
                provider_name,
                tally.last_snapshot_at,
                tally.count,
                tally.error.message,
            )

    def flush_all(self) -> None:
        """Log every provider's outstanding tallies, for a provider whose replay never ended."""
        with self._lock:
            provider_names = tuple(self._tally_by_error_by_provider_name)
        for provider_name in provider_names:
            self.flush_provider(provider_name)


# The line above a traceback's frames: on its own it is the whole headline of a
# bare traceback, and it also opens each traceback chained under a runtime one.
_STDERR_BARE_TRACEBACK_HEADER: Final[str] = "Traceback (most recent call last):"
# The first lines of a multi-line report on the plugin's stderr. The plugin
# folds the ones its serve loop is handed into a line apiece before they ever
# reach here, so what these catch is what its own logging never sees: a crash
# outside it, the interpreter's own output, a plugin build older than that fold.
_STDERR_TRACEBACK_OPENERS: Final[tuple[str, ...]] = (
    _STDERR_BARE_TRACEBACK_HEADER,
    "Task exception was never retrieved",
    "Exception in callback",
    "Unhandled exception in client_connected_cb",
    "+ Exception Group Traceback",
)
# Top-level lines that belong to the report they follow rather than starting
# a message of their own: the object lines asyncio and hypercorn print under
# their headline, the chained-exception separators, and the exception-group
# tree drawing.
_STDERR_TRACEBACK_CONTINUATION_PREFIXES: Final[tuple[str, ...]] = (
    _STDERR_BARE_TRACEBACK_HEADER,
    "future:",
    "handle:",
    "transport:",
    "The above exception was the direct cause",
    "During handling of the above exception",
    "+",
    "|",
)


# How many relayed stderr messages the unexpected-exit report quotes. A
# startup failure is one folded traceback plus the few lines before it, so this
# covers it with room to spare while keeping the report a readable size.
_STDERR_TAIL_LINE_COUNT: Final[int] = 20
# A folded report can still be a long line; the exit report only needs enough
# of each to name the failing step. The whole tail travels as one Sentry value,
# which `setup_sentry` caps in length, so a full tail must fit under that cap or
# the newest line -- the one that matters most -- is the one cut.
_STDERR_TAIL_MAX_LINE_LENGTH: Final[int] = 400
# Once the process has exited its stdout and stderr reach EOF almost at once;
# the bound only matters when something else (a grandchild) still holds a pipe
# open.
_PIPE_DRAIN_TIMEOUT_SECONDS: Final[float] = 2.0
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@pure
def _scrub_stderr_line(line: str, home_dir: str) -> str:
    """Strip what a relayed stderr line may carry about the user before it is quoted in an error report.

    The exit report lands in Sentry, so the tail it quotes drops secret flag
    values the plugin may echo from its argv, URL credentials, email addresses,
    and the home directory (whose path embeds the username).
    """
    secret_flags = "|".join(re.escape(flag) for flag in _SECRET_BEARING_FLAGS)
    scrubbed = re.sub(rf"({secret_flags})(=|\s+)\S+", rf"\1\2{REDACTED_PLACEHOLDER}", line)
    scrubbed = redact_url_credentials_in_text(scrubbed)
    scrubbed = _EMAIL_RE.sub("<email>", scrubbed)
    if home_dir:
        scrubbed = scrubbed.replace(home_dir, "~")
    if len(scrubbed) <= _STDERR_TAIL_MAX_LINE_LENGTH:
        return scrubbed
    # Keep both ends: a folded traceback's exception line is at its end.
    kept_length_per_end = _STDERR_TAIL_MAX_LINE_LENGTH // 2
    return f"{scrubbed[:kept_length_per_end]}...{scrubbed[-kept_length_per_end:]}"


@pure
def _signal_name_of_exit_code(exit_code: int) -> str | None:
    """Name the signal that killed a process from its (negative) ``Popen`` exit code; None for an ordinary exit."""
    if exit_code >= 0:
        return None
    try:
        return signal.Signals(-exit_code).name
    except ValueError:
        return str(-exit_code)


@pure
def _describe_exit_code(exit_code: int) -> str:
    """Render an exit code, naming the signal when the process was killed by one."""
    signal_name = _signal_name_of_exit_code(exit_code)
    if signal_name is None:
        return str(exit_code)
    return f"{exit_code} (signal={signal_name})"


class _ForwardStderrCollapser(MutableModel):
    """Relays the forward's stderr one message per log line, folding tracebacks and counting repeats.

    A traceback the forward's runtime prints is dozens of lines, and a burst of
    identical ones -- 1640 unretrieved-task reports in ninety seconds, when a
    proxy pool timed out on a backlog after a network outage -- relayed line by
    line is what rotated a whole day of the app's log away. The plugin now folds
    and rations what its serve loop is handed, so that storm arrives a line
    apiece; this is the backstop for the reports its own logging never sees. A
    report is folded into one line naming its first and last lines and how many
    were folded, and a report identical to the one just relayed is counted
    rather than relayed, with the count reported when a different message
    arrives or the stream ends. Ordinary single-line messages are relayed as
    they arrive.

    A report under a runtime headline (asyncio's, hypercorn's) runs until the
    next ordinary message, so the chained tracebacks those print -- the pool
    timeout storm was two per report, joined by "The above exception was the
    direct cause" -- fold into the one line. A bare traceback has no headline
    to wait behind and is relayed at its own exception line, so a non-fatal one
    from a plugin that then goes quiet does not wait on a message that may
    never come.

    The last few relayed messages are also kept, scrubbed, so the
    unexpected-exit report can quote what the plugin said before it died: the
    relay itself is at DEBUG and never reaches an error report.
    """

    _open_report: list[str] = PrivateAttr(default_factory=list)
    _is_open_report_traceback_terminated: bool = PrivateAttr(default=False)
    _is_previous_report_line_indented: bool = PrivateAttr(default=False)
    _last_relayed_report_key: tuple[str, str] | None = PrivateAttr(default=None)
    _suppressed_repeat_count: int = PrivateAttr(default=0)
    _home_dir: str = PrivateAttr(default_factory=lambda: str(Path.home()))
    # Appended by the stderr reader thread, read by the lifecycle watcher.
    _tail_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _tail: deque[str] = PrivateAttr(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINE_COUNT))

    def get_tail(self) -> tuple[str, ...]:
        """The last relayed messages, oldest first, scrubbed for quoting in an error report."""
        with self._tail_lock:
            return tuple(self._tail)

    def feed(self, raw_line: str) -> None:
        """Take one stderr line, relaying or folding it."""
        stripped = raw_line.strip()
        if not stripped:
            return
        if self._open_report:
            if self._does_line_continue_the_open_report(raw_line):
                self._open_report.append(stripped)
                self._is_previous_report_line_indented = raw_line[:1].isspace()
                if self._is_open_report_traceback_terminated:
                    self._close_open_report()
                return
            self._close_open_report()
        if stripped.startswith(_STDERR_TRACEBACK_OPENERS):
            self._open_report = [stripped]
            self._is_open_report_traceback_terminated = False
            self._is_previous_report_line_indented = False
            return
        self._relay_suppressed_repeat_count()
        # Only consecutive identical reports are counted: a report that recurs
        # after something else was said is news again.
        self._last_relayed_report_key = None
        self._relay(stripped)

    def flush(self) -> None:
        """Relay whatever is still folded: the stream ended (or the caller is done) and nothing more will close it."""
        if self._open_report:
            self._close_open_report()
        self._relay_suppressed_repeat_count()

    def _does_line_continue_the_open_report(self, raw_line: str) -> bool:
        if raw_line[:1].isspace():
            return True
        stripped = raw_line.strip()
        if stripped.startswith(_STDERR_TRACEBACK_CONTINUATION_PREFIXES):
            return True
        # A headline starts a report of its own however the open one was left:
        # an exception whose message ends on an indented line would otherwise
        # make the rule below read the next report's headline as this report's
        # exception line and swallow it.
        if stripped.startswith(_STDERR_TRACEBACK_OPENERS):
            return False
        # The exception line that ends a traceback is the first un-indented
        # line after its indented frames. It is folded in; a bare traceback is
        # complete at it, while a headlined report stays open for the chained
        # traceback that may follow and closes at the next ordinary message.
        has_seen_traceback_header = any(line.startswith(_STDERR_BARE_TRACEBACK_HEADER) for line in self._open_report)
        if has_seen_traceback_header and self._is_previous_report_line_indented:
            self._is_open_report_traceback_terminated = self._open_report[0].startswith(_STDERR_BARE_TRACEBACK_HEADER)
            return True
        return False

    def _close_open_report(self) -> None:
        report = self._open_report
        self._open_report = []
        self._is_open_report_traceback_terminated = False
        key = (report[0], report[-1])
        if key == self._last_relayed_report_key:
            self._suppressed_repeat_count += 1
            return
        self._relay_suppressed_repeat_count()
        self._last_relayed_report_key = key
        if len(report) == 1:
            self._relay(report[0])
        else:
            self._relay(f"{report[0]} [{len(report) - 1} lines folded, ending: {report[-1]}]")

    def _relay_suppressed_repeat_count(self) -> None:
        if self._suppressed_repeat_count == 0:
            return
        self._relay(f"the previous report repeated {self._suppressed_repeat_count} more time(s)")
        self._suppressed_repeat_count = 0
        self._last_relayed_report_key = None

    def _relay(self, message: str) -> None:
        logger.debug("mngr forward stderr: {}", message)
        scrubbed = _scrub_stderr_line(message, self._home_dir)
        with self._tail_lock:
            self._tail.append(scrubbed)


class EnvelopeStreamConsumer(MutableModel):
    """Owns the ``mngr forward`` subprocess and dispatches its envelope JSONL stream.

    Every public method is safe to call from Imbue Studio's request-handling threads;
    internal state is guarded by ``_lock``.
    """

    resolver: MngrCliBackendResolver = Field(frozen=True, description="Resolver to feed observe + event lines into")
    started_at: datetime = Field(
        frozen=True,
        default_factory=lambda: datetime.now(timezone.utc),
        description=(
            "When this consumer came up. Snapshots finished before this instant "
            "are events-file replay (the pre-start backlog), whose provider "
            "errors describe the gap while Imbue Studio was closed rather than the "
            "present, and are dropped by the observe handler."
        ),
    )
    sleep_tracker: SleepTracker | None = Field(
        default=None,
        frozen=True,
        description=(
            "Records the windows in which this process was not running, so an errored snapshot "
            "whose poll straddled one is treated like the pre-start replay: the error is dropped and "
            "the snapshot advances no freshness. None (tests, embedded factories) fences nothing."
        ),
    )
    is_provider_polled_here: Callable[[str], bool] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Whether discovery on this device still polls a provider instance: an enabled block, or a backend's "
            "own default. Discovery for one it does not is dropped: the events-file replay keeps a removed "
            "instance's (or a suppressed default's) last snapshot in its window forever, and a poll that raced a "
            "block's removal can still report its hosts, either of which would resurrect machines this device can "
            "no longer reach. None (tests, embedded factories) drops nothing."
        ),
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Orders an observe event's vanished-instance check, aggregator apply and
    # resolver provider update against ``forget_provider_instances`` (the
    # materializer's thread). The aggregator orders incremental events against
    # snapshot spans, not snapshots against each other, so a snapshot that passed
    # the check just before its block was deleted would otherwise re-add the
    # instance's hosts after the forget emptied them, and nothing would ever
    # take them away again; its ``update_providers`` after the resolver's own
    # forget would likewise leave a provider entry nothing ever removes.
    _ingest_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # The shared span-aware, per-provider discovery reconciler. Every parsed
    # observe event is folded into it; its accumulated view is what we push into
    # the resolver and fan out as discovered/destroyed callbacks.
    _aggregator: DiscoveryStateAggregator = PrivateAttr(default_factory=DiscoveryStateAggregator)
    # Deduplicates provider-level discovery-error warnings: a provider wedged on
    # the same failure (e.g. missing credentials) logs once per process, not once
    # per poll cycle. Clean snapshots feed it to re-arm on recovery.
    _error_log_suppressor: DiscoveryErrorLogSuppressor = PrivateAttr(default_factory=DiscoveryErrorLogSuppressor)
    # Deduplicates warnings for discovery payloads that do not match this
    # version's schema (the forward envelope replays a shared log that other
    # mngr versions also write).
    _discovery_schema_warner: DiscoverySchemaMismatchWarner = PrivateAttr(
        default_factory=lambda: DiscoverySchemaMismatchWarner(source_description="forward observe stream")
    )
    # Collapses the provider errors dropped while the events-file backlog
    # replays: one snapshot per discovery cycle of downtime, each carrying a
    # wedged-provider error, logs as one counted line per distinct error once
    # that provider's replay ends.
    _pre_start_drop_logger: _PreStartErrorDropLogger = PrivateAttr(default_factory=_PreStartErrorDropLogger)
    # SSH connection info keyed by host id. The aggregator does not model SSH info
    # (it carries no agent/host membership), so it is tracked here and joined onto
    # the agents on each host when building the resolver's view.
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _services_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    # Parallel to _services_by_agent: {agent_id_str: {service_name: registered
    # SVG icon markup}}. Untrusted workspace content, sanitized by consumers.
    _icons_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    # Parallel to _services_by_agent: {agent_id_str: {service_name: how the Share
    # tab offers it}}, for the services whose registration said.
    _share_kinds_by_agent: dict[str, dict[str, ServiceShareKind]] = PrivateAttr(default_factory=dict)
    # Parallel to _services_by_agent: {agent_id_str: {service_name: origin label}}.
    # Carries each service's public origin hostname label (``<name>-<rand>``) so
    # the resolver -- and thus the Share tab -- can build per-service share links.
    _labels_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    # Parallel to _services_by_agent: {agent_id_str: {service_name: display name}}.
    # What users read for each service, so surfaces can call an app what the
    # workspace calls it; a service with none is shown by its service name.
    _display_names_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _on_agent_discovered_callbacks: list[OnAgentDiscoveredCallback] = PrivateAttr(default_factory=list)
    _on_agent_destroyed_callbacks: list[OnAgentDestroyedCallback] = PrivateAttr(default_factory=list)
    _on_system_interface_backend_failure_callbacks: list[OnSystemInterfaceBackendFailureCallback] = PrivateAttr(
        default_factory=list
    )
    _on_system_interface_backend_answered_callbacks: list[OnSystemInterfaceBackendAnsweredCallback] = PrivateAttr(
        default_factory=list
    )
    _on_unexpected_exit_callbacks: list[OnUnexpectedExitCallback] = PrivateAttr(default_factory=list)
    _process: subprocess.Popen[bytes] | None = PrivateAttr(default=None)
    _has_reported_exit: bool = PrivateAttr(default=False)
    _intentional_shutdown: bool = PrivateAttr(default=False)
    # Set once the plugin's port report is settled: it emitted its `listening`
    # envelope (`_listening_port` then holds the port it actually bound), or
    # the lifecycle watcher has logged its exit (`_exit_code` then holds the
    # code), so no port can ever come. `wait_for_listening` blocks on the event
    # so `minds run` can learn the port at startup.
    _listening_settled_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _listening_port: int | None = PrivateAttr(default=None)
    _exit_code: int | None = PrivateAttr(default=None)
    # When `attach` was handed the freshly-spawned subprocess (monotonic clock).
    _attached_at_monotonic: float | None = PrivateAttr(default=None)
    _stderr_collapser: _ForwardStderrCollapser = PrivateAttr(default_factory=_ForwardStderrCollapser)
    # Set once each reader has hit EOF (the stderr one after flushing), so the
    # exit report sees a `listening` envelope and the stderr lines the plugin
    # wrote just before it died.
    _stdout_drained_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stderr_drained_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def add_on_agent_discovered_callback(self, callback: OnAgentDiscoveredCallback) -> None:
        """Register a callback fired for every observe-stream agent discovery."""
        with self._lock:
            self._on_agent_discovered_callbacks.append(callback)

    def add_on_agent_destroyed_callback(self, callback: OnAgentDestroyedCallback) -> None:
        """Register a callback fired for every observe-stream agent destruction."""
        with self._lock:
            self._on_agent_destroyed_callbacks.append(callback)

    def add_on_system_interface_backend_failure_callback(
        self, callback: OnSystemInterfaceBackendFailureCallback
    ) -> None:
        """Register a callback fired for each ``system_interface_backend_failure`` forward-stream envelope.

        The callback receives ``(agent_id, reason, status_code, detail)``.
        ``reason`` is a ``SystemInterfaceBackendFailureReason`` enum value;
        ``status_code`` is set when ``reason`` is ``ERROR_RESPONSE`` (the
        backend's non-2xx status) and ``None`` otherwise; ``detail`` is the
        verbatim error text when the plugin had an exception to quote.
        ``STALLED`` is the one reason that does not report a failed request: it
        means the backend has not answered yet, and the request may still
        succeed. Used by Imbue Studio to feed its ``SystemInterfaceHealthTracker``.
        """
        with self._lock:
            self._on_system_interface_backend_failure_callbacks.append(callback)

    def add_on_system_interface_backend_answered_callback(
        self, callback: OnSystemInterfaceBackendAnsweredCallback
    ) -> None:
        """Register a callback fired for each ``system_interface_backend_answered`` forward-stream envelope.

        The callback receives ``(agent_id, status_code)``: the workspace whose
        shell backend answered a forwarded request, and the 2xx it answered
        with (``None`` for an accepted websocket). The plugin sends at most one
        per agent per few seconds. Used by Imbue Studio to let a live renderer clear a
        health verdict the tracker's own probes cannot.
        """
        with self._lock:
            self._on_system_interface_backend_answered_callbacks.append(callback)

    def add_on_unexpected_exit_callback(self, callback: OnUnexpectedExitCallback) -> None:
        """Register a callback fired once when the plugin subprocess exits unexpectedly.

        The callback receives the subprocess exit code. It fires only for an
        exit Imbue Studio did not ask for (i.e. not after :meth:`terminate`), and at
        most once per consumer. The discovery-health watchdog registers here so
        a dead consumer transitions the app-global state straight to BLOCKED.
        """
        with self._lock:
            self._on_unexpected_exit_callbacks.append(callback)

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        """Store a freshly-spawned plugin subprocess.

        Reader threads are *not* started here -- callers must register
        every callback they need first, then call ``start()`` to begin
        consuming the envelope stream. This avoids a race where envelopes
        arriving between thread start and callback registration would be
        dispatched against an empty callback list.
        """
        if self._process is not None:
            raise EnvelopeStreamConsumerError("EnvelopeStreamConsumer.attach already called")
        self._process = process
        self._attached_at_monotonic = time.monotonic()

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the reader / lifecycle threads for the attached subprocess.

        Must be called after ``attach()`` and after any callbacks that
        need to see the very first envelope have been registered.
        """
        if self._process is None:
            raise EnvelopeStreamConsumerError("EnvelopeStreamConsumer.start called before attach")
        concurrency_group.start_new_thread(
            target=self._read_stdout_loop,
            name="mngr-forward-stdout-reader",
            daemon=True,
            is_checked=False,
        )
        concurrency_group.start_new_thread(
            target=self._read_stderr_loop,
            name="mngr-forward-stderr-reader",
            daemon=True,
            is_checked=False,
        )
        concurrency_group.start_new_thread(
            target=self._wait_and_report_exit,
            name="mngr-forward-lifecycle-watcher",
            daemon=True,
            is_checked=False,
        )

    def wait_for_listening(self, timeout: float) -> int | None:
        """Block until the plugin reports its bound port, or ``timeout`` elapses.

        The plugin emits a single ``listening`` envelope once its FastAPI app
        is ready, carrying the port it actually bound (which differs from the
        default when that port was already in use). Returns that port, or
        ``None`` if no ``listening`` envelope arrived within ``timeout``
        seconds. A subprocess that exits first (it died during startup) can
        never report a port, so this returns ``None``, logging the exit code,
        as soon as the lifecycle watcher has reported the exit rather than
        waiting out the rest of ``timeout``. Returning only after that report
        means a caller that then calls ``terminate()`` cannot turn it into an
        intentional shutdown.

        Must be called after ``start()``; otherwise the reader thread that
        consumes the envelope stream is not running and this always times out.
        """
        if not self._listening_settled_event.wait(timeout=timeout):
            return None
        with self._lock:
            port = self._listening_port
        exit_code = self._exit_code
        if port is None and exit_code is not None:
            logger.warning(
                "mngr forward exited with code {} before reporting a listening port; not waiting out "
                "the rest of the {:.0f}s listen timeout",
                _describe_exit_code(exit_code),
                timeout,
            )
        return port

    def terminate(self) -> None:
        """Stop the plugin subprocess (SIGTERM, then SIGKILL on timeout).

        Sets ``_intentional_shutdown`` *before* signalling the subprocess
        so the lifecycle watcher (``_wait_and_report_exit``) does not report
        the resulting exit to the watchdog as a dead pipeline.
        """
        # A provider that never delivered a post-start snapshot (discovery wedged
        # for the whole session) still has its replay tallies open; log them now
        # rather than losing what its backlog said.
        self._pre_start_drop_logger.flush_all()
        process = self._process
        if process is None:
            return
        self._intentional_shutdown = True
        try:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
        except OSError as e:
            logger.trace("Error terminating plugin subprocess: {}", e)

    def _read_stdout_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            self._handle_envelope_line(line)
        self._stdout_drained_event.set()

    def _read_stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        collapser = self._stderr_collapser
        for raw in process.stderr:
            collapser.feed(raw.decode("utf-8", errors="replace").rstrip("\n"))
        collapser.flush()
        self._stderr_drained_event.set()

    def _wait_and_report_exit(self) -> None:
        process = self._process
        if process is None:
            return
        exit_code = process.wait()
        self._exit_code = exit_code
        # The pipes can still hold a `listening` envelope or the traceback
        # naming the failing step when `wait()` returns; let the readers finish.
        is_stdout_drained = process.stdout is None or self._stdout_drained_event.wait(_PIPE_DRAIN_TIMEOUT_SECONDS)
        is_stderr_drained = process.stderr is None or self._stderr_drained_event.wait(_PIPE_DRAIN_TIMEOUT_SECONDS)
        try:
            # If Imbue Studio asked the subprocess to stop (lifespan shutdown), the exit is
            # expected -- not a dead pipeline.
            if self._intentional_shutdown:
                logger.debug("mngr forward exited with code {} after intentional shutdown", exit_code)
                return
            # Any unasked-for exit means the consumer (and thus the discovery
            # pipeline + traffic proxy) is down. Report it once to the watchdog,
            # which owns the user-facing surfacing (the app-global BLOCKED screen).
            if self._has_reported_exit:
                return
            self._has_reported_exit = True
            self._log_unexpected_exit(exit_code, is_stdout_drained, is_stderr_drained)
        finally:
            self._listening_settled_event.set()
        with self._lock:
            callbacks = list(self._on_unexpected_exit_callbacks)
        for callback in callbacks:
            try:
                callback(exit_code)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_unexpected_exit callback failed: {}", e)

    def _log_unexpected_exit(self, exit_code: int, is_stdout_drained: bool, is_stderr_drained: bool) -> None:
        """Report an unasked-for exit with enough context to tell a startup failure from a runtime crash.

        The classifying facts go in the message itself, which is all a Sentry
        breadcrumb keeps; the stderr tail travels only as a structured field,
        because the plugin's lines are free text that would otherwise mangle
        the event's message.
        """
        attached_at = self._attached_at_monotonic
        seconds_since_spawn = round(time.monotonic() - attached_at, 1) if attached_at is not None else None
        with self._lock:
            had_reported_listening = self._listening_port is not None
        stderr_tail = self._stderr_collapser.get_tail()
        logger.bind(
            exit_code=exit_code,
            signal=_signal_name_of_exit_code(exit_code),
            had_reported_listening=had_reported_listening,
            seconds_since_spawn=seconds_since_spawn,
            is_stdout_drained=is_stdout_drained,
            is_stderr_drained=is_stderr_drained,
            stderr_tail="\n".join(stderr_tail),
        ).error(
            "mngr forward exited unexpectedly with code {}; had_reported_listening={} seconds_since_spawn={} "
            "stderr_tail_lines={}",
            _describe_exit_code(exit_code),
            had_reported_listening,
            seconds_since_spawn,
            len(stderr_tail),
        )

    def _handle_envelope_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.warning("Could not parse envelope line {!r}: {}", stripped[:200], e)
            return
        if not isinstance(envelope, dict):
            return
        stream = envelope.get("stream")
        agent_id_value = envelope.get("agent_id")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return
        if stream == "observe":
            self._handle_observe_payload(payload)
        elif stream == "event":
            if isinstance(agent_id_value, str):
                self._handle_event_payload(AgentId(agent_id_value), payload)
        elif stream == "forward":
            self._handle_forward_payload(payload)
        else:
            logger.trace("Unknown envelope stream {!r}", stream)

    def _did_poll_straddle_a_sleep(self, event: ProviderDiscoverySnapshotEvent) -> bool:
        """Whether this process stopped running somewhere inside the poll's own window."""
        if self.sleep_tracker is None:
            return False
        return self.sleep_tracker.was_asleep_during(event.discovery_started_at, event.discovery_finished_at)

    def _handle_observe_payload(self, payload: dict[str, Any]) -> None:
        # Re-serialize to a single-line JSON so we can reuse mngr's parser.
        try:
            line = json.dumps(payload, separators=(",", ":"))
            event = self._discovery_schema_warner.parse(line)
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse observe payload: {}", e)
            return
        if event is None:
            return
        # The legacy global snapshot is superseded by per-provider snapshots, which
        # the shared aggregator reconciles; Imbue Studio drops the legacy event here too
        # (consuming both would double-count agents during the transition window).
        if isinstance(event, FullDiscoverySnapshotEvent):
            logger.trace("Ignoring legacy full discovery snapshot; Imbue Studio consumes per-provider snapshots")
            return
        with self._ingest_lock:
            unpolled_provider_name = self._unpolled_provider_name(event)
            if unpolled_provider_name is not None:
                logger.info(
                    "Dropping a {} discovery event for provider {}: this device does not poll it",
                    event.type,
                    unpolled_provider_name,
                )
                return
            # SSH info carries no agent/host membership, so the aggregator does not
            # model it; record it here before folding the event in so the agents view
            # we push below joins the info onto the agents on this host.
            if isinstance(event, HostSSHInfoEvent):
                self._record_host_ssh_info(event)
            delta = self._aggregator.apply_event(event)
            if isinstance(event, ProviderDiscoverySnapshotEvent):
                # A clean snapshot re-arms the provider's error-log suppression (and
                # logs a recovery line if its error was previously logged).
                self._error_log_suppressor.record_provider_snapshot(event)
                # A pre-start snapshot is events-file replay, and its error describes
                # the gap while Imbue Studio was closed, not the present: the detached
                # discovery producer keeps polling after the quit flow stops the
                # docker state container, so the backlog's last docker snapshot
                # reliably carries "state container is stopped" -- already outdated,
                # since Imbue Studio restarts that container before this consumer runs.
                # Drop the stale error (a genuinely-broken provider re-asserts it on
                # the first fresh cycle); the snapshot's topology still merges below.
                # The backlog holds one snapshot per cycle of downtime, so the drops
                # are tallied by a collapser rather than logged one line apiece. Only
                # a post-start snapshot ends the replay and logs what it dropped: a
                # clean pre-start one must not, or a provider whose backlog errors
                # flap (as they do across a multi-day gap) logs afresh after every
                # clean cycle in between.
                error = event.error
                # Dropping the error must also drop the snapshot's claim to freshness.
                # ``last_snapshot_at`` is what tells a consumer "discovery has reported this
                # provider", and the cloud-tile state reads a recorded time plus no error as
                # proof the provider is healthy -- so recording it here would launder a
                # provider that was broken for the whole gap into a healthy one reporting
                # zero hosts, and every workspace on it would render "unreachable" (a
                # positive claim that discovery looked and the host was gone) instead of
                # "connecting" (we do not know yet). That is exactly what happened to a
                # leased, perfectly healthy staging host whose provider had been failing for
                # ~25h. Leave freshness unset until a genuine post-start snapshot lands.
                is_snapshot_state_current = True
                if event.discovery_finished_at < self.started_at:
                    if error is not None:
                        self._pre_start_drop_logger.record_dropped_error(
                            provider_name=event.provider_name,
                            error=error,
                            snapshot_at=event.discovery_finished_at,
                        )
                        error = None
                        is_snapshot_state_current = False
                else:
                    self._pre_start_drop_logger.flush_provider(event.provider_name)
                    # A poll that straddled a sleep is the same case inside one
                    # lifetime: its error describes a window in which this process
                    # -- and the network in front of it -- was not there, not the
                    # provider. The socket it timed out on was opened before the lid
                    # closed and read after it opened, so what it reports is that
                    # the laptop went away, and consuming that as the provider's
                    # last word blames a backend that was never asked. Every reader
                    # of provider freshness is fenced here, at the ingress, rather
                    # than each remembering to ask the tracker. A clean poll that
                    # spanned the same sleep completed after the wake and is kept.
                    if error is not None and self._did_poll_straddle_a_sleep(event):
                        logger.info(
                            "Dropping provider error for {} from a discovery poll that straddled a sleep ({} to {}): {}",
                            event.provider_name,
                            event.discovery_started_at.isoformat(),
                            event.discovery_finished_at.isoformat(),
                            error.message,
                        )
                        error = None
                        is_snapshot_state_current = False
                # A per-provider snapshot is also a discovery event, so update_providers
                # bumps last_event_at; merge just this provider's state + freshness.
                # A clean snapshot additionally carries its full host-id set (with the
                # unknown-state hosts, whose absence proves nothing) so the resolver
                # can authoritatively prune this provider's vanished hosts from the
                # last-good topology; an errored snapshot passes None -- its hosts
                # are unreachable, not absent.
                clean_snapshot_host_ids: tuple[str, ...] | None = None
                if event.error is None:
                    clean_snapshot_host_ids = tuple(
                        {str(host.host_id) for host in event.hosts}
                        | {str(host_id) for host_id in event.unknown_host_ids}
                    )
                self.resolver.update_providers(
                    provider_name=event.provider_name,
                    provider=event.provider,
                    error=error,
                    last_snapshot_at=event.discovery_finished_at,
                    clean_snapshot_host_ids=clean_snapshot_host_ids,
                    is_snapshot_state_current=is_snapshot_state_current,
                    unread_host_ids=tuple(str(host_id) for host_id in event.unknown_host_ids),
                )
            else:
                self._record_incremental_event(event)
        self._push_agents_view()
        self._fire_membership_delta(delta)
        # A HostSSHInfoEvent changes no membership (empty delta), so the agents on
        # the host were already announced without SSH info; re-announce them now
        # that their host's SSH info is known.
        if isinstance(event, HostSSHInfoEvent):
            self._refire_discovered_for_host(str(event.host_id))

    def forget_provider_instances(self, provider_names: AbstractSet[str]) -> None:
        """Drop the aggregator's membership for provider instances that no longer exist on this device.

        The resolver forgets them on its own, but the aggregator is the source of
        the agents view pushed on every later event, so without this the next push
        would hand the forgotten hosts straight back. An empty, error-free
        snapshot is the aggregator's own authoritative "this provider has nothing".
        """
        if not provider_names:
            return
        now = datetime.now(timezone.utc)
        # The block is already gone from the settings file when this runs, so once
        # the applies below hold the ingest lock, any event for the instance that
        # the reader thread has not yet folded in fails its vanished check.
        with self._ingest_lock:
            deltas = [
                self._aggregator.apply_event(
                    make_provider_discovery_snapshot_event(
                        provider_name=ProviderInstanceName(name),
                        agents=(),
                        hosts=(),
                        discovery_started_at=now,
                        discovery_finished_at=now,
                    )
                )
                for name in sorted(provider_names)
            ]
        delta_removed_agents: list[AgentInstanceKey] = []
        for delta in deltas:
            delta_removed_agents.extend(delta.removed_agent_instances)
            self._fire_membership_delta(delta)
        if delta_removed_agents:
            logger.info(
                "Forgot {} agent(s) of provider instances that no longer exist here", len(delta_removed_agents)
            )
        self._push_agents_view()

    def _unpolled_provider_name(self, event: DiscoveryEvent) -> str | None:
        """The event's provider when discovery on this device does not poll it, else None."""
        if self.is_provider_polled_here is None:
            return None
        if isinstance(event, ProviderDiscoverySnapshotEvent):
            provider_name = str(event.provider_name)
        elif isinstance(event, AgentDiscoveryEvent):
            provider_name = str(event.agent.provider_name)
        elif isinstance(event, HostDiscoveryEvent):
            provider_name = str(event.host.provider_name)
        elif isinstance(event, DiscoveryErrorEvent) and event.provider_name is not None:
            provider_name = event.provider_name
        else:
            return None
        if self.is_provider_polled_here(provider_name):
            return None
        return provider_name

    def _record_incremental_event(self, event: DiscoveryEvent) -> None:
        """Log a discovery error (if any) and bump the resolver's last-event time for a non-snapshot event.

        Incremental events (agent/host discovered or destroyed, SSH info, errors)
        are not snapshots, so they advance only ``last_event_at`` -- the
        per-provider snapshot freshness is bumped solely by ``update_providers``.
        """
        if isinstance(event, DiscoveryErrorEvent):
            self._error_log_suppressor.log_discovery_error_event(event)
        self.resolver.record_discovery_event_received(datetime.now(timezone.utc))

    def _build_agents_result(self) -> ParsedAgentsResult:
        """Project the aggregator's agents + hosts (joined with SSH info) into a ParsedAgentsResult.

        The aggregator is the source of truth for agent / host membership and host
        lifecycle state. SSH info is tracked here (the aggregator does not model
        it) and joined onto each agent via its host id.
        """
        agents = self._aggregator.get_agents()
        hosts = self._aggregator.get_hosts()
        with self._lock:
            ssh_by_host_id = dict(self._ssh_by_host_id)
        ssh_info_by_agent_id = {
            str(agent.agent_id): ssh_by_host_id[str(agent.host_id)]
            for agent in agents
            if str(agent.host_id) in ssh_by_host_id
        }
        host_state_by_host_id = {str(host.host_id): host.host_state for host in hosts if host.host_state is not None}
        # Capture every known host's normalized name (regardless of whether its state
        # is known) so the resolver's display-name fallback and host-name collision
        # checks have it.
        host_name_by_host_id = {str(host.host_id): str(host.host_name) for host in hosts}
        host_size_by_host_id = {str(host.host_id): host.size for host in hosts if host.size is not None}
        return ParsedAgentsResult(
            agent_ids=tuple(agent.agent_id for agent in agents),
            discovered_agents=tuple(agents),
            ssh_info_by_agent_id=ssh_info_by_agent_id,
            host_state_by_host_id=host_state_by_host_id,
            host_name_by_host_id=host_name_by_host_id,
            host_size_by_host_id=host_size_by_host_id,
        )

    def _push_agents_view(self) -> None:
        """Push the aggregator's current agents / hosts view into the resolver."""
        self.resolver.update_agents(self._build_agents_result())

    def _fire_membership_delta(self, delta: AggregatorDelta) -> None:
        """Fire destroyed / discovered callbacks for the agents the delta removed or added.

        A removed agent also has its accumulated service map cleared from both this
        consumer and the resolver. A discovered agent's SSH info is looked up from
        its host (populated by a prior ``HostSSHInfoEvent``, if any). A removed
        host's cached SSH info is forgotten so the map does not grow without bound.
        """
        agent_by_instance = self._aggregator.get_agent_by_instance()
        for host_id_str in delta.removed_host_ids:
            with self._lock:
                self._ssh_by_host_id.pop(host_id_str, None)
        for instance_key in delta.removed_agent_instances:
            # Imbue Studio's own mirrors stay keyed by the bare agent id (its workspace
            # identity); the delta is instance-scoped, so extract the id here.
            agent_id = instance_key.agent_id
            with self._lock:
                self._services_by_agent.pop(str(agent_id), None)
                self._labels_by_agent.pop(str(agent_id), None)
                self._icons_by_agent.pop(str(agent_id), None)
                self._share_kinds_by_agent.pop(str(agent_id), None)
                self._display_names_by_agent.pop(str(agent_id), None)
            self.resolver.update_services(agent_id, {})
            self._fire_destroyed(agent_id)
        for instance_key in delta.added_agent_instances:
            agent = agent_by_instance.get(instance_key)
            if agent is None:
                continue
            self._fire_discovered(agent.agent_id, self._ssh_for_host(str(agent.host_id)), str(agent.provider_name))

    def _record_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        """Store the SSH connection info carried by a HostSSHInfoEvent, keyed by host id."""
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user,
            host=event.ssh.host,
            port=event.ssh.port,
            key_path=event.ssh.key_path,
            known_hosts_path=event.ssh.known_hosts_path,
        )
        with self._lock:
            self._ssh_by_host_id[str(event.host_id)] = ssh_info

    def _refire_discovered_for_host(self, host_id_str: str) -> None:
        """Re-announce every agent on a host, carrying the host's now-known SSH info."""
        ssh_info = self._ssh_for_host(host_id_str)
        for agent in self._aggregator.get_agents():
            if str(agent.host_id) == host_id_str:
                self._fire_discovered(agent.agent_id, ssh_info, str(agent.provider_name))

    def _ssh_for_host(self, host_id_str: str) -> RemoteSSHInfo | None:
        """Return the SSH connection info recorded for ``host_id_str``, or None."""
        with self._lock:
            return self._ssh_by_host_id.get(host_id_str)

    def _fire_discovered(
        self,
        agent_id: AgentId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
    ) -> None:
        with self._lock:
            callbacks = list(self._on_agent_discovered_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id, ssh_info, provider_name)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_agent_discovered callback failed for {}: {}", agent_id, e)

    def _fire_destroyed(self, agent_id: AgentId) -> None:
        with self._lock:
            callbacks = list(self._on_agent_destroyed_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_agent_destroyed callback failed for {}: {}", agent_id, e)

    def _handle_event_payload(self, agent_id: AgentId, payload: dict[str, Any]) -> None:
        source = payload.get("source", "")
        aid_str = str(agent_id)
        if source != SERVICES_EVENT_SOURCE_NAME:
            return
        try:
            record = parse_service_log_record(payload)
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse service event for {}: {}", agent_id, e)
            return
        with self._lock:
            services = self._services_by_agent.setdefault(aid_str, {})
            labels = self._labels_by_agent.setdefault(aid_str, {})
            icons = self._icons_by_agent.setdefault(aid_str, {})
            share_kinds = self._share_kinds_by_agent.setdefault(aid_str, {})
            display_names = self._display_names_by_agent.setdefault(aid_str, {})
            if isinstance(record, ServiceDeregisteredRecord):
                services.pop(str(record.service), None)
                labels.pop(str(record.service), None)
                icons.pop(str(record.service), None)
                share_kinds.pop(str(record.service), None)
                display_names.pop(str(record.service), None)
            else:
                services[str(record.service)] = record.url
                if record.label:
                    labels[str(record.service)] = record.label
                else:
                    labels.pop(str(record.service), None)
                if record.icon:
                    icons[str(record.service)] = record.icon
                else:
                    icons.pop(str(record.service), None)
                if record.display_name:
                    display_names[str(record.service)] = record.display_name
                else:
                    display_names.pop(str(record.service), None)
                if record.share_kind is not None:
                    share_kinds[str(record.service)] = record.share_kind
                else:
                    share_kinds.pop(str(record.service), None)
            services_snapshot = dict(services)
            labels_snapshot = dict(labels)
            icons_snapshot = dict(icons)
            display_names_snapshot = dict(display_names)
            share_kinds_snapshot = dict(share_kinds)
        self.resolver.update_services(
            agent_id, services_snapshot, labels_snapshot, icons_snapshot, display_names_snapshot, share_kinds_snapshot
        )

    def _handle_forward_payload(self, payload: dict[str, Any]) -> None:
        payload_type = payload.get("type")
        if payload_type == "reverse_tunnel_established":
            logger.trace("Ignoring reverse_tunnel_established envelope: {}", payload)
        elif payload_type == "system_interface_backend_failure":
            try:
                agent_id = AgentId(str(payload["agent_id"]))
                raw_reason = str(payload["reason"])
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Could not parse system_interface_backend_failure payload: {}", e)
                return
            reason = _parse_backend_failure_reason(raw_reason)
            status_code = _parse_status_code(payload)
            raw_detail = payload.get("detail")
            detail = str(raw_detail) if raw_detail is not None else None
            with self._lock:
                callbacks = list(self._on_system_interface_backend_failure_callbacks)
            for callback in callbacks:
                try:
                    callback(agent_id, reason, status_code, detail)
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("system_interface_backend_failure callback failed for {}: {}", agent_id, e)
        elif payload_type == "system_interface_backend_answered":
            try:
                agent_id = AgentId(str(payload["agent_id"]))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Could not parse system_interface_backend_answered payload: {}", e)
                return
            status_code = _parse_status_code(payload)
            with self._lock:
                callbacks = list(self._on_system_interface_backend_answered_callbacks)
            for callback in callbacks:
                try:
                    callback(agent_id, status_code)
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("system_interface_backend_answered callback failed for {}: {}", agent_id, e)
        elif payload_type == "listening":
            self._handle_listening(payload)
        elif payload_type == "login_url":
            logger.debug("Forward stream payload {}: {}", payload_type, payload)
        else:
            logger.trace("Unknown forward payload type {!r}", payload_type)

    def _handle_listening(self, payload: dict[str, Any]) -> None:
        """Record the plugin's bound port from a ``listening`` envelope.

        Fires ``_listening_settled_event`` so a ``wait_for_listening`` caller unblocks.
        A malformed payload is logged and dropped -- the waiter keeps waiting
        rather than proceeding with a bogus port.
        """
        raw_port = payload.get("port")
        if raw_port is None:
            logger.warning("`listening` envelope is missing its port: {}", payload)
            return
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            logger.warning("Could not parse port from `listening` envelope: {}", payload)
            return
        with self._lock:
            self._listening_port = port
        self._listening_settled_event.set()
        logger.info("`mngr forward` is listening on port {}", port)


def start_mngr_forward(
    config: ForwardSubprocessConfig,
    resolver: MngrCliBackendResolver,
    sleep_tracker: SleepTracker | None = None,
    is_provider_polled_here: Callable[[str], bool] | None = None,
) -> tuple[EnvelopeStreamConsumer, str, str]:
    """Spawn the ``mngr forward`` subprocess and attach an envelope consumer.

    Returns ``(consumer, preauth_cookie_value, browser_bridge_token)``. The
    reader threads are *not* started yet -- the caller MUST:

    1. register its on_agent_discovered / on_agent_destroyed handlers
       on the consumer;
    2. call ``consumer.start(concurrency_group)`` to begin consuming
       envelopes;
    3. hand the preauth cookie to the Electron shell so it can pre-set
       ``mngr_forward_session=<value>`` on ``localhost:<port>`` before the
       first agent-subdomain navigation. The browser bridge token backs the
       ``/forward-bridge`` route, browser mode's twin of that injection.

    Splitting attach (here) from start (caller) avoids a race where
    envelopes arriving before the caller has registered its callbacks
    would be dispatched against an empty callback list and silently
    dropped.
    """
    preauth_cookie = secrets.token_urlsafe(_PREAUTH_TOKEN_LENGTH)
    browser_bridge_token = secrets.token_urlsafe(_PREAUTH_TOKEN_LENGTH)
    command = _build_forward_command(config, preauth_cookie, browser_bridge_token)
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(config.mngr_host_dir)
    logger.info("Spawning `mngr forward` subprocess: {}", " ".join(_redact_secrets(command)))
    # noqa: S603 — command is fully controlled (mngr binary + the args we
    # build above), no untrusted input reaches the argv list.
    process = subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=env,
        cwd=str(Path.home()),
    )
    consumer = EnvelopeStreamConsumer(
        resolver=resolver, sleep_tracker=sleep_tracker, is_provider_polled_here=is_provider_polled_here
    )
    consumer.attach(process)
    return consumer, preauth_cookie, browser_bridge_token


def _build_forward_command(
    config: ForwardSubprocessConfig,
    preauth_cookie: str,
    browser_bridge_token: str,
) -> list[str]:
    """Build the ``mngr forward`` argv for the subprocess Imbue Studio spawns."""
    command: list[str] = [
        config.mngr_binary,
        "forward",
        "--host",
        "127.0.0.1",
        "--service",
        config.service,
        # Tail the shared discovery log written by the single `mngr observe` under
        # `mngr latchkey forward` rather than spawning a second discovery observer.
        "--observe-via-file",
        "--preauth-cookie",
        preauth_cookie,
        "--browser-bridge-token",
        browser_bridge_token,
        "--format",
        "jsonl",
    ]
    # TLS + HTTP/2 so the workspace origin is not capped by Chromium's
    # per-origin HTTP/1.1 connection limit. The Electron shell trusts the
    # proxy's CA-signed leaf for its loopback origins programmatically.
    command.append("--use-http2")
    for include in config.agent_include:
        command.extend(["--agent-include", include])
    for spec in config.reverse_specs:
        command.extend(["--reverse", spec])
    for origin in config.embedder_origins:
        command.extend(["--embedder-origin", origin])
    if config.request_headers_file is not None:
        command.extend(["--request-headers-file", str(config.request_headers_file)])
    return command


def _redact_secrets(command: list[str]) -> list[str]:
    """Return a copy of ``command`` with secret-bearing argument values masked for logging.

    The actual ``Popen`` call uses the unredacted list so the plugin still
    receives the real values. Today we redact the ``--preauth-cookie`` value
    (a freshly-minted shared secret between Imbue Studio, the plugin, and the
    Electron shell); future secret-bearing flags can be added to
    ``_SECRET_BEARING_FLAGS``.
    """
    return redact_secret_flag_values(command, secret_bearing_flags=_SECRET_BEARING_FLAGS)


_SECRET_BEARING_FLAGS: Final[tuple[str, ...]] = ("--preauth-cookie", "--browser-bridge-token")
