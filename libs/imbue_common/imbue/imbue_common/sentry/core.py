import functools
import gzip
import os
import re
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Hashable
from collections.abc import Mapping
from collections.abc import Sequence
from enum import StrEnum
from functools import cache
from functools import partial
from pathlib import Path
from typing import Any
from typing import Final
from typing import Iterable
from typing import MutableMapping
from typing import TypedDict
from typing import cast

import sentry_sdk
import sentry_sdk.serializer
import sentry_sdk.utils
import traceback_with_variables
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from sentry_sdk import HttpTransport
from sentry_sdk import get_current_scope
from sentry_sdk.consts import EndpointType
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.integrations.stdlib import StdlibIntegration
from sentry_sdk.types import Event
from sentry_sdk.types import Hint
from traceback_with_variables import Format

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.sentry.data_types import LogAttachmentGroup
from imbue.imbue_common.sentry.loguru_handler import SENTRY_LOG_FORMAT
from imbue.imbue_common.sentry.loguru_handler import SentryBreadcrumbHandler
from imbue.imbue_common.sentry.loguru_handler import SentryEventHandler
from imbue.imbue_common.sentry.loguru_handler import SentryLoguruLoggingLevels
from imbue.imbue_common.sentry.loguru_handler import log_error_inside_sentry
from imbue.imbue_common.sentry.loguru_handler import should_record_sentry_event
from imbue.imbue_common.sentry.s3_uploader import EXTRAS_UPLOADED_FILES_KEY
from imbue.imbue_common.sentry.s3_uploader import get_s3_upload_key
from imbue.imbue_common.sentry.s3_uploader import get_s3_upload_url
from imbue.imbue_common.sentry.s3_uploader import setup_s3_uploads
from imbue.imbue_common.sentry.s3_uploader import upload_to_s3
from imbue.imbue_common.sentry.s3_uploader import upload_to_s3_with_key
from imbue.imbue_common.sentry.s3_uploader import wait_for_s3_uploads

# suffix appended to the (gzip-compressed) S3 upload keys for log files
COMPRESSED_LOG_EXTENSION = "gz"

# The suffix of an upload key whose object this uploader gzipped on the way up.
_GZIPPED_UPLOAD_KEY_SUFFIX: Final[str] = f".{COMPRESSED_LOG_EXTENSION}"

# Suffixes naming bytes that already carry their own compression: an upload of one keeps that suffix
# on its key and is stored verbatim. Same reasoning the ``*.gz`` log attachment groups are declared
# with ``is_compressed=False`` -- re-compressing buys nothing, and it would force whoever follows the
# uri to unwrap two layers to reach the archive (or log) they asked for.
_ALREADY_COMPRESSED_SUFFIXES: Final[tuple[str, ...]] = (".zip", _GZIPPED_UPLOAD_KEY_SUFFIX)


def _is_already_compressed(filename_or_suffix: str) -> bool:
    """Whether the bytes a filename (or a bare suffix) names already carry their own compression."""
    return filename_or_suffix.lower().endswith(_ALREADY_COMPRESSED_SUFFIXES)


def _upload_key_suffix(filename_or_suffix: str) -> str:
    """The suffix an S3 upload key must carry for bytes named (or suffixed) ``filename_or_suffix``.

    The one place that choice is made, so a key always advertises the encoding of the object under it:
    already-compressed bytes keep their own suffix, and everything else is gzipped on the way up and
    keyed ``.gz``.
    """
    lowered = filename_or_suffix.lower()
    for suffix in _ALREADY_COMPRESSED_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return _GZIPPED_UPLOAD_KEY_SUFFIX


def _is_gzipped_upload_key(key: str) -> bool:
    """Whether the object at ``key`` must be gzip, judged by the suffix the key itself advertises.

    An upload to an already-published key takes its instruction from the key rather than from the file
    it is reading, so the bytes stored there cannot contradict the uri a reader follows.
    """
    return key.lower().endswith(_GZIPPED_UPLOAD_KEY_SUFFIX)


# sentry's size limits are annoyingly hard to evaluate before sending the event. we'll just try to be conservative.
# https://docs.sentry.io/concepts/data-management/size-limits/
# https://develop.sentry.dev/sdk/data-model/envelopes/#size-limits
MAX_SENTRY_ATTACHMENT_SIZE = 10 * 1024 * 1024
# How many files one log attachment group sweeps onto an event, and hence the length of the uri list
# under that group's ``uploaded_files_<group>`` extra. A volume cap of our own, independent of the
# SDK-side databag trimming that ``raise_sentry_databag_breadth`` lifts.
MAX_SENTRY_LIST_SIZE = 10

# The sentry SDK's serializer trims every "databag" node -- the event's ``extra`` dict itself, and
# each dict/list nested anywhere under it -- to its first ``MAX_DATABAG_BREADTH`` entries in
# insertion order, recording only a ``{"len": N}`` marker in the event's ``_meta`` for the rest. The
# SDK default of 10 is smaller than one bug report's worth of extras (the ``bug_report`` payload,
# the description uri, ten swept log groups, and the reserved report-file uris), which would
# silently drop the keys inserted last -- the report's own attachment pointers. There is no
# ``sentry_sdk.init`` option for this; the serializer reads the module-level constant live, so it is
# raised to a value comfortably above any real event while still bounding a pathological one.
MAX_SENTRY_DATABAG_BREADTH: Final[int] = 100


def raise_sentry_databag_breadth() -> None:
    """Lift sentry's client-side databag truncation to ``MAX_SENTRY_DATABAG_BREADTH`` entries per node.

    Called from ``setup_sentry`` before any event can be serialized (and mirrored by the capturing
    test client, so tests see the same serializer behavior production runs under). The attribute is
    asserted to exist so an SDK upgrade that renames it fails loudly here instead of silently
    reinstating the 10-entry cap.
    """
    assert hasattr(sentry_sdk.serializer, "MAX_DATABAG_BREADTH"), (
        "sentry_sdk.serializer no longer defines MAX_DATABAG_BREADTH; re-port the databag-breadth raise"
    )
    # The SDK ships the constant unannotated, so the checker narrows it to Literal[10]; the attribute
    # is genuinely mutable at runtime (the serializer reads it live on every serialize call).
    sentry_sdk.serializer.MAX_DATABAG_BREADTH = MAX_SENTRY_DATABAG_BREADTH  # ty: ignore[invalid-assignment]


# The Sentry scope context key under which the per-process config (the log folder
# used for attachment collection) is stored.
_SENTRY_CONFIG_CONTEXT_KEY = "_config"


# S3 key prefix, and the ``event["extra"]`` key, for the verbatim copy of a manual bug report's
# description. It shares the ``uploaded_files_`` naming of the log attachments so it sorts alongside
# them in the Sentry UI. See ``submit_manual_bug_report`` for why the description is sent out of band.
BUG_REPORT_DESCRIPTION_KEY_PREFIX = "bug_report_description"
BUG_REPORT_DESCRIPTION_EXTRA_KEY = f"{EXTRAS_UPLOADED_FILES_KEY}_{BUG_REPORT_DESCRIPTION_KEY_PREFIX}"

# First fingerprint component on every manually-submitted bug report; the second is what makes each
# report its own issue. Sentry does not surface fingerprints in the issue list, so this does nothing
# for findability (the ``MANUALLY_SUBMITTED_TAG`` tag is what these are searchable by) -- it is there
# to namespace the uuid when reading raw event JSON.
_MANUAL_BUG_REPORT_FINGERPRINT_PREFIX = "manual-bug-report"


class SentryEventRejected(Exception):
    pass


class ExceptionKey(FrozenModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    exception_type: type[BaseException] | None
    exception_args: tuple[Hashable, ...]

    @classmethod
    def build_from_exception_or_fingerprint(
        cls, exception: BaseException | None, log_fingerprint: str | None
    ) -> "ExceptionKey":
        if exception is None:
            return cls(
                exception_type=None,
                exception_args=(log_fingerprint,),
            )
        else:
            return cls(
                exception_type=type(exception),
                # FIXME: we may grab things with references here unnecessarily. Let's store only the hash here and stringified representation.
                exception_args=tuple(arg for arg in exception.args if isinstance(arg, Hashable)),
            )


class ExceptionHistory(MutableModel):
    total_sent: int = 0
    total_throttled: int = 0

    # monotonic clock value
    last_reported_at: float | None = None
    throttled_since_last_report: int = 0

    @property
    def since_last_report(self) -> float:
        last_reported_at = self.last_reported_at
        if last_reported_at is None:
            return float("inf")
        return time.monotonic() - last_reported_at

    def log_throttled(self):
        self.throttled_since_last_report += 1
        self.total_throttled += 1

    def log_reported(self):
        self.last_reported_at = time.monotonic()
        self.throttled_since_last_report = 0
        self.total_sent += 1


def _first_line_of_log_message(event: Event) -> str | None:
    """Extracts the first line of the log message from the event, if any."""
    message = event.get("logentry", {}).get("message")
    if message and isinstance(message, str):
        message_lines = message.strip().splitlines()
        if message_lines:
            return message_lines[0]
    return None


def _get_full_location_from_event(event: Event) -> str | None:
    """Extracts the `full_location` field that we are supposed to generate in our log handlers."""
    outer_extra = event.get("extra")
    if not isinstance(outer_extra, dict):
        return None
    extra = cast(dict[str, Any], outer_extra).get("extra")
    if isinstance(extra, dict):
        full_location = cast(dict[str, Any], extra).get("full_location")
        if full_location and isinstance(full_location, str):
            return full_location.strip() or None
    return None


class _ReasonToAllowSendingEvent(StrEnum):
    PASS_THRU = "pass_thru"
    NO_RATE_LIMIT_INFO = "no_rate_limit_info"
    TOO_MANY_TRACKED_EXCEPTIONS = "too_many_tracked_exceptions"
    INITIAL = "initial"
    INITIAL_GRACE_PERIOD = "initial_grace_period"
    TIMEOUT_ELAPSED = "timeout_elapsed"


class _SentryEventRateLimiter(MutableModel):
    """Prevent logging the same specific exceptions multiple times to sentry.

    Each allowed exception is assumed to be sent.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # these exception will never be rate limited
    pass_thru_exception_types: Collection[type[BaseException]] = Field(default_factory=set)
    # the number of initial reports to allow before starting to apply rate limiting
    initial_reports_without_rate_limiting: int = 2
    # the time (in seconds) that must pass since the last report of a given exception before allowing
    # another report it is multiplied by the number of times the exception has been passed-thru since
    # the app start after the first throttling event
    timeout_factor: float = 60.0
    # maximum number of different exceptions to track for rate limiting
    # once this number is exceeded, all events will be passed through unfiltered
    max_tracked_rate_limited_exceptions: int = 10_000

    # we should not be called in parallel, but better safe than sorry
    # this lock protects access to _exception_history, its contents, and the total counters
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _exception_history: MutableMapping[ExceptionKey, ExceptionHistory] = PrivateAttr(default_factory=dict)
    _total_throttled: int = PrivateAttr(default=0)
    _total_sent: int = PrivateAttr(default=0)

    def _annotate_event(
        self, event: Event, reason_to_allow: _ReasonToAllowSendingEvent, past_history: ExceptionHistory | None = None
    ) -> Event:
        logger.trace("Annotating event with rate limiter: {}", reason_to_allow)

        annotation: dict[str, Any] = {
            "reason_to_allow": reason_to_allow.value,
            "application": {
                "total_throttled": self._total_throttled,
                "total_sent": self._total_sent,
                # thread-safe to read without lock since we don't care about consistency
                "total_tracked": len(self._exception_history),
            },
        }
        if past_history is not None:
            annotation["instance"] = {
                "since_last_report": past_history.since_last_report,
                "throttled_since_last_report": past_history.throttled_since_last_report,
                "total_throttled": past_history.total_throttled,
                "total_sent": past_history.total_sent,
            }

        event.setdefault("extra", {})
        event["extra"]["rate_limiter"] = annotation

        event.setdefault("tags", {})
        event["tags"]["rate_limiter_reason_to_allow"] = reason_to_allow
        return event

    def before_send(self, event: Event, hint: Hint) -> Event | None:
        annotated_event = self._before_send(event, hint)
        with self._lock:
            if annotated_event is None:
                self._total_throttled += 1
            else:
                self._total_sent += 1

        return annotated_event

    def _before_send(self, event: Event, hint: Hint) -> Event | None:
        exception = None
        exception_type = None
        # see sentry_sdk._types.ExcInfo which sadly we can't import
        if "exc_info" in hint:
            exception_type, exception, _ = hint["exc_info"]

        if (exception_type is not None) and (exception_type in self.pass_thru_exception_types):
            return self._annotate_event(event, _ReasonToAllowSendingEvent.PASS_THRU)

        first_line = _first_line_of_log_message(event)
        full_location = _get_full_location_from_event(event)
        if first_line and full_location:
            log_fingerprint = "\n".join([first_line, full_location])
        else:
            log_fingerprint = None

        if not (log_fingerprint or exception):
            # nothing to rate limit on
            return self._annotate_event(event, _ReasonToAllowSendingEvent.NO_RATE_LIMIT_INFO)

        key = ExceptionKey.build_from_exception_or_fingerprint(exception, log_fingerprint)
        with self._lock:
            if key not in self._exception_history:
                # we could LRU but if we got to this point, there's something else to figure out, like bad keying
                if len(self._exception_history) >= self.max_tracked_rate_limited_exceptions:
                    return self._annotate_event(event, _ReasonToAllowSendingEvent.TOO_MANY_TRACKED_EXCEPTIONS)
                history = ExceptionHistory(last_reported_at=time.monotonic(), total_sent=1)
                self._exception_history[key] = history
                return self._annotate_event(event, _ReasonToAllowSendingEvent.INITIAL)

            history = self._exception_history[key]
            reason_to_allow: _ReasonToAllowSendingEvent | None = None
            if history.total_sent < self.initial_reports_without_rate_limiting:
                reason_to_allow = _ReasonToAllowSendingEvent.INITIAL_GRACE_PERIOD
            else:
                current_timeout = self.timeout_factor * max(
                    1, history.total_sent - self.initial_reports_without_rate_limiting + 1
                )
                if history.since_last_report >= current_timeout:
                    logger.trace("Timeout elapsed for event: {}, {}", key, current_timeout)
                    reason_to_allow = _ReasonToAllowSendingEvent.TIMEOUT_ELAPSED

            if reason_to_allow:
                event = self._annotate_event(event, reason_to_allow=reason_to_allow, past_history=history)
                history.log_reported()
                return event
            history.log_throttled()

        logger.trace("Rate limiting event: {}", key)
        return None


class ImbueSentryHttpTransport(HttpTransport):
    """The sentry python sdk has pretty lame behavior if the event is too large.
    It'll just drop it, and record stats indicating that an event was dropped.
    You can see these in the Sentry org's stats page, category "invalid".
    But there's no way to recover any information about the dropped event.

    We could try to just ensure the events don't violate the size limit, which we try to do,
    but their size limits are a bit complicated and thus hard to pre-verify. So we also want to know if anything slips through.

    The actual sentry web API does return a status code (413) if the event was rejected,
    so we need to handle this at the level of the sentry HttpTransport and do something with it.
    """

    def _send_request(
        self,
        body: bytes,
        headers: dict[str, str],
        endpoint_type: EndpointType = EndpointType.ENVELOPE,
        envelope: Envelope | None = None,
    ) -> None:
        """This is a copy of the original `_send_request` method from the HttpTransport class,
        with a hook to call `on_too_large_event` added.
        """

        def record_loss(reason: str) -> None:
            if envelope is None:
                self.record_lost_event(reason, data_category="error")
            else:
                envelope_items = envelope.items
                assert envelope_items is not None
                for item in envelope_items:
                    self.record_lost_event(reason, item=item)

        headers.update(
            {
                "User-Agent": str(self._auth.client),
                "X-Sentry-Auth": str(self._auth.to_header()),
            }
        )
        try:
            response = self._request(
                "POST",
                endpoint_type,
                body,
                headers,
            )
        except Exception:
            self.on_dropped_event("network")
            record_loss("network_error")
            raise

        try:
            self._update_rate_limits(response)

            if response.status == 429:
                # if we hit a 429.  Something was rate limited but we already
                # acted on this in `self._update_rate_limits`.  Note that we
                # do not want to record event loss here as we will have recorded
                # an outcome in relay already.
                self.on_dropped_event("status_429")

            elif response.status >= 300 or response.status < 200:
                sentry_sdk.utils.logger.error(
                    "Unexpected status code: %s (body: %s)",
                    response.status,
                    getattr(response, "data", getattr(response, "content", None)),
                )
                self.on_dropped_event("status_{}".format(response.status))
                record_loss("network_error")

                if response.status == 413:
                    assert envelope is not None
                    self.on_too_large_event(body, envelope)
        finally:
            response.close()

    def on_too_large_event(self, body: bytes, envelope: Envelope) -> None:
        """we want to log _something_ to sentry, because otherwise we have no idea what happened,
        but we also need to be super careful that this fallback doesn't itself fail.

        exceptions raised here will simply get eaten and result in nothing getting logged to sentry,
        both due to sentry's usage of `capture_internal_exceptions`
        and that we're running in a worker thread and i don't think they make an effort to re-surface exceptions from threads.
        """
        msg = "request was too large to send to sentry"
        try:
            raise SentryEventRejected(msg)
        except SentryEventRejected as e:
            stripped_envelope = Envelope(headers=envelope.headers)
            attachment_sizes = {}
            envelope_items = envelope.items
            assert envelope_items is not None
            for item in envelope_items:
                if item.data_category == "attachment":
                    payload = item.payload
                    payload_bytes_len = len(payload.get_bytes() if not isinstance(payload, (bytes, str)) else payload)
                    item_headers = item.headers
                    assert item_headers is not None
                    attachment_sizes[item_headers["filename"]] = payload_bytes_len
                    continue
                stripped_envelope.add_item(item)
            # this is uncompressed (so we can inspect it)
            serialized_stripped_envelope = stripped_envelope.serialize()

            extra: dict[str, str | int] = {
                "uncompressed_attachment_sizes": str(attachment_sizes),
                "original_compressed_request_body_size": len(body),
                "uncompressed_stripped_envelope_size": len(serialized_stripped_envelope),
            }

            # send stripped envelope to S3 -- is preceding code now overkill?
            upload_name = upload_to_s3("stripped_envelope", ".txt", serialized_stripped_envelope)

            log_error_inside_sentry(e, msg, extra=extra, additional_s3_uploads=(upload_name,) if upload_name else None)


def get_traceback_with_vars(exception: BaseException | None = None) -> str:
    # be careful of potential performance regressions with increasing these limits
    tb_format = Format(max_value_str_len=100_000, max_exc_str_len=2_000_000)
    if exception is None:
        # no exception passed in; get the current exception. this will still be None if not in an exception handler
        exception = sys.exception()
    try:
        if exception is not None:
            # we are in an exception handler, use that for the traceback
            # for some reason this breaks when casting to an `Exception`, so just using type: ignore
            return traceback_with_variables.format_exc(exception, fmt=tb_format)
        else:
            # not in an exception handler, just get the current stack
            return traceback_with_variables.format_cur_tb(fmt=tb_format)
    except Exception as e:
        return f"got exception while formatting traceback with `traceback_with_variables`: {traceback.format_exception(e)}"


# We define BeforeSendType here to be one or more callables that match the signature of sentry's before_send hook.
# The event will be passed through each one in our wrapping code.
BaseBeforeSendType = Callable[[Event, Hint], Event | None]


# Events carrying this tag are user-submitted bug reports, which the user explicitly asked to have
# sent (an explicit user action), so the automatic-error gate below lets them through even when
# automatic error reporting is turned off.
MANUALLY_SUBMITTED_TAG = "manually_submitted"


class _AutomaticReportingGate(MutableModel):
    """before_send hook (a callable object, mirroring ``_SentryEventRateLimiter``) that drops automatic
    events while error reporting is disabled.

    ``is_error_reporting_enabled`` is read live on every event, so toggling the source of that flag
    takes effect without restarting. Events tagged ``MANUALLY_SUBMITTED_TAG`` always pass: a manual
    bug report is an explicit user action.
    """

    is_error_reporting_enabled: Callable[[], bool]

    def before_send(self, event: Event, hint: Hint) -> Event | None:
        tags = event.get("tags") or {}
        if isinstance(tags, dict) and tags.get(MANUALLY_SUBMITTED_TAG) == "true":
            return event
        if self.is_error_reporting_enabled():
            return event
        return None


def _make_automatic_reporting_gate(is_error_reporting_enabled: Callable[[], bool]) -> BaseBeforeSendType:
    """Build the automatic-reporting before_send gate bound to a live ``is_error_reporting_enabled``."""
    return _AutomaticReportingGate(is_error_reporting_enabled=is_error_reporting_enabled).before_send


def _drop_interrupt_events(event: Event, hint: Hint) -> Event | None:
    """before_send hook that drops interrupt / clean-shutdown exceptions, which are not real faults.

    ``KeyboardInterrupt`` (Ctrl-C / SIGINT) is always dropped: it is not itself an error. A
    ``SystemExit`` is dropped only for a clean exit code (``None`` or ``0``); a non-zero code is a
    genuine fatal-exit signal and is kept.

    The ``SentryEventHandler`` already filters these out of the *logging* path, but the SDK's default
    excepthook / threading integrations capture every top-level ``BaseException`` and call
    ``capture_event`` directly, bypassing that handler. ``before_send`` is the one place every event
    passes through regardless of which integration produced it, so the filter belongs here. Any *other*
    exception raised during shutdown has a different type and is left untouched, so genuine errors are
    still reported.
    """
    if "exc_info" not in hint:
        return event
    exc_type, exc_value, _ = hint["exc_info"]
    if exc_type is None:
        return event
    if issubclass(exc_type, KeyboardInterrupt):
        return None
    if issubclass(exc_type, SystemExit):
        code = exc_value.code if isinstance(exc_value, SystemExit) else None
        if code is None or code == 0:
            return None
    return event


# NOTE: if the actual event (without attachments) being too large is a problem, then it will be handled
#       in our custom logic in ImbueSentryHttpTransport above.
def _before_send_wrapper(
    event: Event,
    hint: Hint,
    before_send_list: Iterable[BaseBeforeSendType],
) -> Event | None:
    try:
        result = event
        for before_send in before_send_list:
            maybe_event = before_send(result, hint)
            if maybe_event is None:
                return None
            result = maybe_event
        return result
    except Exception as e:
        # It is critical that we catch errors here, because this runs inside Sentry's before_send hook.
        # Failing to report the failure means we would see NOTHING about it.
        #
        # ``log_error_inside_sentry`` both records the failure in the local app log (so it is never lost)
        # and reports it to Sentry via a minimal event on a cleared scope. It is non-reentrant, so even
        # though reporting re-runs this same before_send chain, a deterministic before_send failure cannot
        # recurse: the nested report is dropped.
        log_error_inside_sentry(e, "Failure when processing event in before_send hook")
        # NOTE: this re-raise will get suppressed by Sentry and treated as if `before_send` returned `None`
        raise


def _register_ignored_loggers(ignored_loggers: Sequence[str]) -> None:
    """Tell the default ``LoggingIntegration`` to drop records from the given stdlib loggers.

    ``ignore_logger`` mutates a module-level registry the integration reads live (via ``fnmatch``),
    so each name may be a glob and the effect applies to both events and breadcrumbs. Needed because
    the integration patches ``logging.Logger.callHandlers`` at the class level and thus captures a
    logger's ERROR records as events even when that logger has ``propagate=False`` -- which would
    otherwise flood Sentry with handled third-party noise (e.g. paramiko SSH banner errors).
    """
    for ignored_logger in ignored_loggers:
        ignore_logger(ignored_logger)


def fixup_release_id(release_id: str) -> str:
    """
    For pre-release release candidate versions, Sentry requires the release ID to be in the semver format.

    E.g. "0.1.0rc1" should be converted to "0.1.0-rc.1".

    """
    return re.sub(r"(\d+\.\d+\.\d+)rc(\d+)", r"\1-rc.\2", release_id)


# A persisted anonymous user id is a 32-char lowercase hex string (``uuid4().hex``). Kept opaque and
# free of any PII: it exists only so Sentry can count distinct affected installs per issue.
_ANONYMOUS_USER_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def get_or_create_anonymous_user_id(id_file_path: Path) -> str:
    """Read (or create-and-persist) a stable, random anonymous user id at ``id_file_path``.

    The id is a random ``uuid4`` hex string carrying no PII. It is attached to every Sentry event
    (via ``sentry_sdk.set_user``) so Sentry can report the number of distinct installs affected by a
    given issue -- letting us tell a rare bug that hits many users apart from a noisy one that hits a
    single user.

    When the file is absent it is created atomically with ``O_EXCL`` so two processes racing on first
    launch (e.g. the minds backend and the Electron main process) converge on a single id: the loser
    of the race sees ``FileExistsError`` and reads the winner's value. A file that exists but holds a
    blank/malformed value is overwritten atomically (temp file + rename) with a freshly generated id.
    """
    for _attempt in range(2):
        try:
            existing = id_file_path.read_text().strip()
            does_file_exist = True
        except OSError:
            existing = ""
            does_file_exist = False
        if _ANONYMOUS_USER_ID_PATTERN.fullmatch(existing):
            return existing
        new_id = uuid.uuid4().hex
        id_file_path.parent.mkdir(parents=True, exist_ok=True)
        if does_file_exist:
            # The file exists but holds a corrupt/blank value: overwrite it atomically. (This is not a
            # first-launch race -- the file already existed -- so there is no id to converge on.)
            tmp_path = id_file_path.with_suffix(".tmp")
            tmp_path.write_text(new_id)
            tmp_path.rename(id_file_path)
            return new_id
        try:
            # File absent: ``x`` (O_EXCL) fails if another process created it first; then we loop and
            # read that process's value so racing processes converge on a single id.
            with open(id_file_path, "x") as id_file:
                id_file.write(new_id)
            return new_id
        except FileExistsError:
            continue
    # Extremely unlikely: the file kept appearing/vanishing between our read and create on both
    # attempts. Fall back to an in-memory id so reporting still carries *some* user rather than
    # crashing setup.
    return uuid.uuid4().hex


def setup_sentry(
    dsn: str,
    # The Sentry environment label (e.g. ``production`` / ``staging`` / ``development``). Which Sentry
    # project a ``dsn`` points at, and which environment names exist, is project-specific config the
    # caller supplies; this library is agnostic to it.
    environment_name: str,
    release_id: str,
    git_commit_sha: str,
    log_folder: Path,
    # Distinguishes which Imbue Python process produced an event when several
    # report to the same Sentry project (e.g. ``minds-backend`` vs.
    # ``mngr-latchkey-forward``). Recorded as the ``service`` tag and the event
    # ``server_name``.
    service_name: str,
    # A stable, randomly-generated anonymous user id (no PII) attached to every event via
    # ``sentry_sdk.set_user`` so Sentry can count the distinct installs affected by each issue. The
    # caller persists this (see ``get_or_create_anonymous_user_id``) and shares one value across all
    # processes of a single install (the minds backend, the ``mngr latchkey forward`` daemon, and the
    # JS frontends) so an install is counted once regardless of which process reported the event.
    user_id: str,
    log_attachment_groups: Sequence[LogAttachmentGroup],
    integrations: Sequence[Integration],
    is_error_reporting_enabled: Callable[[], bool],
    # The S3 bucket to upload log/traceback attachments to, or ``None`` to disable S3 entirely (e.g.
    # in environments that have no bucket). When set, the uploader is initialized for this bucket;
    # attachments are still only collected per-event while ``is_error_reporting_enabled`` returns True.
    s3_attachment_bucket: str | None = None,
    extra_tags: Mapping[str, str] | None = None,
    # Glob patterns (matched via ``fnmatch``) for stdlib logger names whose records must never
    # become Sentry events or breadcrumbs. The default ``LoggingIntegration`` patches
    # ``logging.Logger.callHandlers`` at the class level, so it captures ERROR-level stdlib records
    # as events even for loggers whose ``propagate`` is disabled. Callers that already route a noisy
    # third-party logger's output elsewhere (e.g. mngr redirects paramiko/pyinfra into loguru) pass
    # those logger names here so Sentry drops the raw records instead of flooding on handled noise.
    ignored_loggers: Sequence[str] = (),
) -> None:
    """Sets up the main Sentry instance for this process.

    This should be done *after* setting up normal loguru loggers, to ensure that sentry handling happens after normal logging.
    In case the sentry stuff hangs or something odd, we want to make sure to at least get regular log output.

    Sentry always initializes; what it actually *sends* is gated live by ``is_error_reporting_enabled``:

    * It is read on every event (in a before_send hook). While it returns False, automatic events are
      dropped before they leave the process. Manually-submitted bug reports (tagged
      ``MANUALLY_SUBMITTED_TAG``) bypass this gate.
    * It also gates log/traceback attachment collection: while it returns False, no attachments are
      collected for automatic events. Attachments are additionally only uploaded when
      ``s3_attachment_bucket`` is set.

    The callable is read live, so toggling its source takes effect without a restart.

    ``integrations`` are the Sentry integrations to enable in addition to the default integrations
    (e.g. a Flask integration for the minds backend; none for the ``mngr latchkey forward`` daemon).
    """
    if "SENTRY_DSN" in os.environ:
        # We pass ``dsn=`` explicitly below, so sentry_sdk ignores any SENTRY_DSN
        # in the environment. Warn rather than crash: a user may have it set for
        # unrelated reasons.
        logger.info("Ignoring SENTRY_DSN from the environment; the DSN is passed in explicitly.")

    sentry_dsn = dsn

    # Lift the serializer's per-dict/list truncation before anything can be captured: the SDK default
    # keeps fewer extra keys than one event carries, silently dropping whatever was inserted last.
    raise_sentry_databag_breadth()

    # NOTE: the rate limiter object's lifetime is maintained by being captured in the closure of the
    #       before_send function. Interrupt / clean-shutdown exceptions are dropped first (they are
    #       never real faults), then the automatic-reporting gate drops events the user has opted out
    #       of, both before they consume a rate-limiter slot.
    rate_limiter = _SentryEventRateLimiter()
    before_send = functools.partial(
        _before_send_wrapper,
        before_send_list=[
            _drop_interrupt_events,
            _make_automatic_reporting_gate(is_error_reporting_enabled),
            rate_limiter.before_send,
        ],
    )

    sentry_sdk.init(
        sample_rate=1.0,
        environment=environment_name,
        server_name=service_name,
        # We use Sentry for error reporting, not performance monitoring. Leaving
        # tracing on would emit a transaction for every HTTP request (including
        # the long-lived SSE streams and polling), which is high-volume and adds
        # Sentry cost for no benefit here, so disable it.
        traces_sample_rate=0.0,
        # required for `logger.error` calls to include stacktraces
        attach_stacktrace=True,
        # note this will capture unhandled exceptions even if not explicitly logged, among other things
        # https://docs.sentry.io/platforms/python/integrations/default-integrations/
        default_integrations=True,
        # this doesn't affect the default integrations, but prevents any other ones from being added automatically
        auto_enabling_integrations=False,
        integrations=list(integrations),
        disabled_integrations=[StdlibIntegration()],
        dsn=sentry_dsn,
        send_default_pii=False,
        # sentry has a max payload size of 1MB, so we can't make this infinite
        max_value_length=10_000,
        add_full_stack=True,
        before_send=before_send,
        release=fixup_release_id(release_id),
        # default is 100; can't make it too large because total event size must be <1MB
        max_breadcrumbs=100,
        # if the locals is very large, sentry gets to be quite slow to log errors if this is enabled.
        # we log our own traceback_with_variables anyways.
        include_local_variables=False,
        transport=ImbueSentryHttpTransport,
    )
    logger.info("Sentry initialized")

    _register_ignored_loggers(ignored_loggers)

    # The S3 attachment uploader is initialized whenever a bucket is configured. Whether
    # logs/tracebacks are actually collected and uploaded is decided live per-event by
    # ``is_error_reporting_enabled`` (in ``add_extra_info_hook``); with no bucket, nothing is uploaded.
    if s3_attachment_bucket is not None:
        setup_s3_uploads(bucket=s3_attachment_bucket)
        logger.info("Sentry S3 attachment uploader ready (bucket={})", s3_attachment_bucket)
    else:
        logger.info("Sentry S3 attachment uploads disabled (no bucket configured)")

    # Attach the anonymous user id to every event. It is a random, opaque id (no PII), so
    # ``send_default_pii`` stays False (no IP / cookies / real identity collected); only this id is
    # sent, purely so Sentry can count the number of distinct installs affected by each issue.
    scope = get_current_scope()
    scope.set_user({"id": user_id})

    # Bind the log-attachment uploader for this process's log layout, and register it (so manual bug
    # reports can reach it) plus the loguru handler that turns errors/exceptions into Sentry events.
    attachments_uploader = ErrorAttachmentsS3Uploader(log_attachment_groups=tuple(log_attachment_groups))
    register_attachments_uploader(attachments_uploader)
    add_extra_info_hook_partial = partial(add_extra_info_hook, is_error_reporting_enabled=is_error_reporting_enabled)

    min_sentry_level: int = SentryLoguruLoggingLevels.LOW_PRIORITY.value
    handler = SentryEventHandler(
        level=min_sentry_level,
        add_extra_info_hook=add_extra_info_hook_partial,
    )
    register_sentry_event_handler(handler)
    logger.add(
        handler,
        level=min_sentry_level,
        diagnose=False,
        format=SENTRY_LOG_FORMAT,
        # records explicitly marked to skip Sentry (e.g. the local app-log line emitted by
        # log_error_inside_sentry) must reach the file sinks but never become Sentry events themselves.
        filter=should_record_sentry_event,
    )
    # capture lower level loguru messages to add as breadcrumbs on events
    # the extra info is not helpful here and makes the breadcrumbs larger; they're still available in the log file attachment
    breadcrumb_level: int = SentryLoguruLoggingLevels.INFO.value
    logger.add(
        SentryBreadcrumbHandler(level=breadcrumb_level, strip_extra=True),
        level=breadcrumb_level,
        diagnose=False,
        format=SENTRY_LOG_FORMAT,
    )
    scope.set_context(
        _SENTRY_CONFIG_CONTEXT_KEY,
        # need to cast to `dict` to make PyCharm happy
        cast(
            dict,
            SentryConfigDict(
                log_folder_path=log_folder,
            ),
        ),
    )
    scope.set_tag("git_sha", git_commit_sha)
    scope.set_tag("service", service_name)
    if extra_tags is not None:
        for tag_name, tag_value in extra_tags.items():
            scope.set_tag(tag_name, tag_value)
    logger.info("Sentry initialized with DSN: {}", sentry_dsn)
    logger.info("Sentry initialized with log folder: {}", log_folder)


_SENTRY_EVENT_HANDLER: SentryEventHandler | None = None


def register_sentry_event_handler(handler: SentryEventHandler) -> None:
    global _SENTRY_EVENT_HANDLER
    _SENTRY_EVENT_HANDLER = handler


def get_sentry_event_handler() -> SentryEventHandler | None:
    return _SENTRY_EVENT_HANDLER


_ATTACHMENTS_UPLOADER: "ErrorAttachmentsS3Uploader | None" = None


def register_attachments_uploader(uploader: "ErrorAttachmentsS3Uploader | None") -> None:
    """Set (or, with None, clear) the process-wide uploader that attachments are collected through."""
    global _ATTACHMENTS_UPLOADER
    _ATTACHMENTS_UPLOADER = uploader


def get_attachments_uploader() -> "ErrorAttachmentsS3Uploader | None":
    return _ATTACHMENTS_UPLOADER


# Keep this short: it runs on a process's shutdown path, so a wedged or
# unreachable Sentry/S3 endpoint must not stall exit for long.
_SHUTDOWN_FLUSH_TIMEOUT_SECONDS: float = 3.0


def flush_sentry_on_shutdown(timeout: float = _SHUTDOWN_FLUSH_TIMEOUT_SECONDS) -> None:
    """Flush Sentry and its pending attachment uploads before the process exits.

    Called from a process's teardown so errors captured late in the session are
    not lost. The order matters: first drain the loguru handler's add-extra-info
    callbacks (they enqueue the S3 attachment uploads), then wait for the S3
    uploader's own pool to finish (so the URLs already referenced in captured
    events resolve), then flush the Sentry client so queued events are actually
    sent.

    The timeout is intentionally short so an unreachable Sentry/S3 endpoint can
    only briefly delay shutdown. Safe to call when Sentry was never set up: each
    step no-ops on an uninitialized client.
    """
    handler = get_sentry_event_handler()
    if handler is not None:
        handler.close()
    wait_for_s3_uploads(timeout=timeout, is_shutting_down=True)
    sentry_sdk.flush(timeout=timeout)


class SentryConfigDict(TypedDict):
    log_folder_path: Path | None


def _get_config_from_scope() -> SentryConfigDict:
    scope = get_current_scope()._contexts.get(_SENTRY_CONFIG_CONTEXT_KEY, SentryConfigDict(log_folder_path=None))
    # we only put SentryConfigDict in _contexts, but regrettably as a third-party library we can't tell the checker that
    return cast(SentryConfigDict, scope)


def _get_log_folder_from_scope() -> Path | None:
    log_folder_path = _get_config_from_scope().get("log_folder_path")
    if log_folder_path and log_folder_path.exists():
        logger.debug("Using Sentry context log_folder_path: {}", str(log_folder_path))
        return log_folder_path
    logger.info("No log file path found")
    return None


@cache
def _get_platform_info() -> str:
    return sys.platform


def _file_size_or_unknown(path: Path) -> int | str:
    """The file's size for logging, or a marker when it cannot be read.

    Sizing a log purely to report it must never be what fails an error report.
    """
    try:
        return path.stat().st_size
    except OSError:
        return "unknown"


def _n_newest_files(files: Iterable[Path], n: int) -> Iterable[Path]:
    assert n > 0
    return sorted(files, key=lambda f: f.stat().st_mtime)[-n:]


# Callbacks returned by ``collect_external_attachments``: each is a pre-bound
# ``functools.partial`` that performs one S3 upload when invoked with no arguments.
_UploadCallback = Callable[[], None]


def _bug_report_description_bytes(description: str) -> bytes:
    """Encode a bug report description for its out-of-band upload.

    ``backslashreplace`` because JSON can carry a lone surrogate that plain utf-8 refuses to encode:
    it would raise ``UnicodeEncodeError``, which -- on the path where the uploads run inline rather
    than on the handler's executor -- propagates out of the submit and costs the whole report.
    Escaping also keeps the character legible instead of flattening it to ``?``.
    """
    return description.encode("utf-8", errors="backslashreplace")


class ErrorAttachmentsS3Uploader(MutableModel):
    """Collects (and uploads) everything an error report carries out of band: its log files, its
    traceback, and -- for a manual bug report -- the user's own description.

    The set of log files is driven by ``log_attachment_groups``: each group's glob
    is matched under the process's log folder (or the group's ``base_dir``, when
    set), the newest matches are kept, and immutable groups (e.g. rotated logs)
    are uploaded once and their S3 key cached.
    """

    # The per-process log layout to attach. Empty means only the (logsite)
    # traceback is uploaded.
    log_attachment_groups: tuple[LogAttachmentGroup, ...] = Field(default_factory=tuple)

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # stores all previously uploaded immutable (e.g. rotated) logs by path
    _immutable_logs_keys: dict[Path, str] = PrivateAttr(default_factory=dict)

    @staticmethod
    def _upload_traceback_cb(key: str, exception: BaseException | None) -> None:
        tb_with_vars = get_traceback_with_vars(exception)
        if tb_with_vars is not None:
            upload_to_s3_with_key(key, tb_with_vars.encode())

    def _upload_file_cb(self, key: str, file_path: Path, compress: bool = False, immutable: bool = False) -> None:
        contents = file_path.read_bytes()
        if compress:
            # The highest compression level that still uses the fast pass implementation.
            # https://github.com/madler/zlib/blob/5a82f71ed1dfc0bec044d9702463dbdf84ea3b71/deflate.c#L117
            contents = gzip.compress(contents, compresslevel=3)
        uri = upload_to_s3_with_key(key, contents)
        # Only cache immutable (e.g. rotated) files, whose contents never change, so a
        # later error report can reuse the same key instead of re-uploading them.
        if uri is not None and immutable:
            with self._lock:
                # we assume that uri and key are in sync
                self._immutable_logs_keys[file_path] = key

    @staticmethod
    def _upload_description_cb(key: str, description: str) -> None:
        upload_to_s3_with_key(key, _bug_report_description_bytes(description))

    def prepare_description_upload(self, description: str) -> tuple[str | None, _UploadCallback]:
        """Prepare the verbatim, out-of-band upload of a manual bug report's description.

        Returns the ``s3://`` URI the text will be readable at (None when S3 uploads are not
        configured) and the callback that performs the upload. Unlike the log attachments, this is
        never compressed -- it is short, and a reader following the URI from the Sentry event wants to
        read it directly.
        """
        key = get_s3_upload_key(BUG_REPORT_DESCRIPTION_KEY_PREFIX, ".txt")
        return get_s3_upload_url(key), partial(self._upload_description_cb, key=key, description=description)

    def prepare_report_file_uploads(
        self, file_paths: Mapping[str, Path]
    ) -> tuple[Mapping[str, Collection[str | None]], tuple[_UploadCallback, ...]]:
        """Prepare one-shot uploads of files that belong to exactly one manual report.

        The log attachment groups are process-global and swept onto *every*
        event, which is right for rolling logs and wrong for files a user
        consented to attach to one specific bug report: anything a group's glob
        can match rides along on every unrelated error event until it is
        deleted. These uploads instead name exact files, produce the same
        ``uploaded_files_<name>`` extras shape, and are referenced by nothing
        else -- staged report files can therefore sit on disk untouched without
        ever appearing on another event.

        Both the key's suffix and whether the bytes are gzipped come from the
        staged file's own name, so an attachment that already carries its own
        compression (the chat-transcript ``.zip``) is stored verbatim under a key
        that says so.
        """
        uris: dict[str, Collection[str | None]] = {}
        callbacks: tuple[_UploadCallback, ...] = ()
        for name, file_path in file_paths.items():
            if not file_path.is_file():
                continue
            logger.info(
                "Sentry attachment selected for report file {}: {} ({} bytes)",
                name,
                file_path,
                _file_size_or_unknown(file_path),
            )
            key = get_s3_upload_key(file_path.name, _upload_key_suffix(file_path.name))
            uris[name] = [get_s3_upload_url(key)]
            callbacks += (
                partial(
                    self._upload_file_cb,
                    key=key,
                    file_path=file_path,
                    compress=not _is_already_compressed(file_path.name),
                ),
            )
        return uris, callbacks

    def reserve_report_file_uploads(
        self, staged_suffix_by_name: Mapping[str, str]
    ) -> Mapping[str, tuple[str | None, str]]:
        """Reserve an S3 key (and the URI it will be readable at) per attachment name, uploading nothing.

        Keys are minted purely locally -- a timestamp and a uuid4 -- so a report can publish where its
        attachments *will* live and be captured immediately, while collecting them takes tens of
        seconds. Each entry is ``(uri, key)``: the uri goes on the event, and the key is what
        ``upload_reserved_report_file`` writes to once the bytes exist. The uri is None exactly when no
        bucket is configured, which is the same condition under which the upload is a no-op.

        Each name is given the suffix its file will be staged under (``.zip`` for the chat-transcript
        archive, ``.log`` for a log) rather than the staged filename, because nothing is staged yet:
        the name itself does not exist either, carrying the slug of a collection that has not started.
        The suffix is the whole of what a key needs -- it is what the key advertises, and what the
        upload later reads back off the key to decide whether to gzip.

        A name whose file never materializes (dropped for secrets, or a collection that failed) leaves
        its reserved object absent; the report's status document is what says why.
        """
        # Bytes that are already gzip have no key to reserve: theirs would carry the same ``.gz``
        # suffix as a key this uploader compresses into, leaving the upload no way to tell the two
        # apart and so no way to avoid double-compressing them.
        assert all(suffix.lower() != _GZIPPED_UPLOAD_KEY_SUFFIX for suffix in staged_suffix_by_name.values()), (
            f"cannot reserve upload keys for already-gzipped bytes: {dict(staged_suffix_by_name)}"
        )
        return {
            name: self._reserve_upload(name, _upload_key_suffix(staged_suffix))
            for name, staged_suffix in staged_suffix_by_name.items()
        }

    def reserve_text_upload(self, name: str) -> tuple[str | None, str]:
        """Reserve a key for a plain-text document (e.g. a report's attachment status) as ``(uri, key)``.

        Separate from ``reserve_report_file_uploads`` because the text is uploaded uncompressed, so its
        key must not claim the ``.gz`` suffix that a reader follows the uri with.
        """
        return self._reserve_upload(name, ".txt")

    @staticmethod
    def _reserve_upload(key_prefix: str, key_suffix: str) -> tuple[str | None, str]:
        key = get_s3_upload_key(key_prefix, key_suffix)
        return get_s3_upload_url(key), key

    def upload_reserved_report_file(self, key: str, file_path: Path) -> None:
        """Upload ``file_path`` to a key reserved earlier by ``reserve_report_file_uploads``.

        Whether the bytes are gzipped is read off the reserved key's own suffix rather than off the
        file, so the object can never contradict the uri that already went out on the event: a ``.gz``
        key is one this uploader compresses into, and any other suffix it reserves (today ``.zip``)
        holds the staged bytes verbatim. The one-shot path decides the same way from the same suffixes,
        so which of the two published a uri makes no difference to whoever follows it.

        A missing file is logged and otherwise ignored: an
        attachment that was dropped (for secrets, or by a failed collection) simply never arrives, and
        raising here would take down the rest of a report's uploads with it.
        """
        if not file_path.is_file():
            logger.info("Sentry attachment never materialized for reserved key {}: {}", key, file_path)
            return
        logger.info(
            "Sentry attachment selected for report file {}: {} ({} bytes)",
            key,
            file_path,
            _file_size_or_unknown(file_path),
        )
        self._upload_file_cb(key=key, file_path=file_path, compress=_is_gzipped_upload_key(key))

    @staticmethod
    def upload_reserved_text(key: str, text: str) -> None:
        """Upload arbitrary text to a previously reserved key, uncompressed.

        Mirrors ``_upload_description_cb`` (including its lenient encoding, since any text routed here
        may carry a lone surrogate that came in over JSON): a reader following the uri from the Sentry
        event reads the document directly.
        """
        upload_to_s3_with_key(key, _bug_report_description_bytes(text))

    def collect_external_attachments(
        self, *, exception: BaseException | None, logs_folder: Path | None
    ) -> tuple[Mapping[str, Collection[str | None]], tuple[_UploadCallback, ...]]:
        """Prepares external uploads that will be attached to the error report.

        Returns external urls grouped by their logical names and the callbacks that need to be invoked which will
        actually perform the uploads to make those urls available.
        """
        uploads: dict[tuple[str, str], _UploadCallback | None] = {}

        if exception is not None:
            # this traceback is from the logger call site!
            key = get_s3_upload_key("logsite_traceback_with_vars", ".txt")
            uploads[("", key)] = partial(self._upload_traceback_cb, key=key, exception=exception)

        for group in self.log_attachment_groups:
            group_folder = group.base_dir if group.base_dir is not None else logs_folder
            if group_folder is None:
                continue
            self._collect_group_uploads(uploads, group_folder, group)

        grouped_uris: defaultdict[str, list[str | None]] = defaultdict(list)
        for group_name, key in uploads.keys():
            grouped_uris[group_name].append(get_s3_upload_url(key))

        callbacks = tuple(c for c in uploads.values() if c is not None)
        return grouped_uris, callbacks

    def _collect_group_uploads(
        self,
        uploads: dict[tuple[str, str], _UploadCallback | None],
        group_folder: Path,
        group: LogAttachmentGroup,
    ) -> None:
        key_suffix = f".{COMPRESSED_LOG_EXTENSION}" if group.is_compressed else ""
        for log_file in _n_newest_files(group_folder.glob(group.glob), n=group.max_file_count):
            # The selected file, named before anything is keyed or uploaded, so a
            # report's actual attachment set is readable from the logs alone. In
            # an environment with no bucket configured the upload is skipped and
            # the event's URIs are all null, which is otherwise indistinguishable
            # from a group that matched nothing.
            logger.info(
                "Sentry attachment selected for group {}: {} ({} bytes)",
                group.group_name,
                log_file,
                _file_size_or_unknown(log_file),
            )
            if group.is_immutable:
                with self._lock:
                    existing_key = self._immutable_logs_keys.get(log_file)
                if existing_key is not None:
                    logger.trace("Not uploading {} because it already exists under {}", log_file, existing_key)
                    uploads[(group.group_name, existing_key)] = None
                    continue
            key = get_s3_upload_key(log_file.name, key_suffix)
            uploads[(group.group_name, key)] = partial(
                self._upload_file_cb,
                key=key,
                file_path=log_file,
                compress=group.is_compressed,
                immutable=group.is_immutable,
            )

    @staticmethod
    def _wait_for_all_uploads(timeout: float | None) -> bool | None:
        """Only to be used for testing, to avoid coupling tests with the global object"""
        return wait_for_s3_uploads(timeout=timeout, is_shutting_down=False)


def add_extra_info_hook(
    event: Event, hint: Hint, is_error_reporting_enabled: Callable[[], bool]
) -> tuple[Event, Hint, tuple[_UploadCallback, ...]]:
    """The add_extra_info_hook gets called in the SentryEventHandler. This seems a little too early in the process for
    sending things to s3.

    Sentry may still decide to discard the issue and in that scenario, executing all the uploads now would just
    blackhole them.

    Log/traceback attachment collection is gated by ``is_error_reporting_enabled`` (read live): while it
    returns False, the event is dropped anyway, so no log or traceback uploads are prepared and the
    event carries no attachments. The lightweight ``platform`` extra is always added regardless.
    """
    extra = cast(dict[str, Any], event["extra"])

    uploader = get_attachments_uploader()
    if is_error_reporting_enabled() and uploader is not None:
        exception = sys.exception()
        if exception is None:
            try:
                raise Exception("this is an exception to get the current traceback")
            except Exception as e:
                exception = e

        s3_uri_groups, callbacks = uploader.collect_external_attachments(
            exception=exception, logs_folder=_get_log_folder_from_scope()
        )

        if s3_uri_groups:
            for group_name, s3_uris in s3_uri_groups.items():
                # NOTE: EXTRAS_UPLOADED_FILES_KEY is not safe to write to, as it may get stomped by other code paths
                extra_name = f"{EXTRAS_UPLOADED_FILES_KEY}_{group_name}"
                # NOTE: It is possible that there are pre-existing contents of this list that
                #       will bump the list size over the MAX_SENTRY_LIST_SIZE. Ignoring this edge
                #       as no one is expected to actually write to these at the moment of committing this.
                extra[extra_name] = extra.get(extra_name, []) + list(s3_uris)
    else:
        callbacks = ()

    extra["platform"] = _get_platform_info()
    return event, hint, tuple(callbacks)


def submit_manual_bug_report(
    *,
    title: str,
    description: str,
    report: Mapping[str, Any],
    logs_folder: Path | None,
    report_file_paths: Mapping[str, Path] | None = None,
    report_file_uris: Mapping[str, str | None] | None = None,
) -> str | None:
    """Synthesize and send a user-submitted bug report as a Sentry event.

    Unlike automatic error reporting, this is an explicit user action: the event is tagged
    ``MANUALLY_SUBMITTED_TAG`` so the automatic-reporting gate always lets it through, even when
    automatic error reporting is turned off. It is not tied to an exception -- ``title`` becomes the
    event message and ``report`` is attached as structured context.

    ``description`` is the user's own prose. It travels twice: inline inside ``report``, and -- where
    S3 uploads are configured -- as an upload of its own, whose URI the event carries. The inline copy
    is the convenient one to read but cannot be relied on, for two independent reasons. Sentry's
    default data scrubber replaces a whole string value with ``[Filtered]`` whenever the built-in
    password pattern matches anywhere in that value, and because that pattern is a set of plain
    substrings (``auth``, ``secret``, ``token...=``, ...), ordinary prose trips it: the word "authored"
    alone destroyed a 9,705-character report. Separately, ``max_value_length`` truncates it at 10,000
    characters. The uploaded copy is subject to neither.

    When a ``logs_folder`` is given, recent log files are uploaded through the same S3-attachment
    mechanism as automatic errors (a no-op in environments without an S3 bucket). No traceback is
    collected (a manual report has no meaningful one).

    A report's own attachments arrive one of two ways, producing identically-shaped
    ``uploaded_files_<name>`` extras. ``report_file_paths`` names files that already exist and uploads
    them here; ``report_file_uris`` carries uris reserved via ``reserve_report_file_uploads`` for files
    still being collected, letting the event be captured now with the bytes following later. A name
    must not be given both ways, or one would silently stomp the other's uri.

    Returns the Sentry event id (a 32-char hex string the user can quote when following up), or None
    if Sentry is not active or the event was dropped before sending.
    """
    assert not (report_file_paths and report_file_uris and set(report_file_paths) & set(report_file_uris)), (
        "an attachment name may be given either as a path or as a reserved uri, not both"
    )

    client = sentry_sdk.get_client()
    if not client.is_active():
        logger.info("Sentry is not active; manual bug report was not sent")
        return None

    # Build ``extra`` as a local dict (also referenced by the event) so attachment URLs can be added
    # without re-subscripting the loosely-typed Event TypedDict.
    extra: dict[str, Any] = {"bug_report": dict(report)}
    event: Event = {
        "message": title,
        "level": "info",
        "tags": {MANUALLY_SUBMITTED_TAG: "true"},
        "extra": extra,
        # A random fingerprint gives every report its own Sentry issue. Sentry's default grouping is
        # driven by the stack trace, which here is the submitting code path and therefore identical
        # for every report, so distinct titles do not split them: all reports land in one catch-all
        # issue whose per-issue verbs (assign, resolve, comment, link) cannot address a single report.
        # Grouping genuine duplicates would buy nothing either -- two reports of one underlying bug
        # are still two people to answer. The cost: a submit whose response is lost after the event
        # was captured (a dropped connection, or a later 500) leaves the form resubmittable, so a
        # retry files a second issue where it used to join the first.
        "fingerprint": [_MANUAL_BUG_REPORT_FINGERPRINT_PREFIX, uuid.uuid4().hex],
    }

    # Extras are inserted most-important-first: anything that trims an extra dict (the client-side
    # serializer should its lifted cap ever regress, or a server-side limit) keeps the first entries
    # and drops the last, so the report's own attachment pointers go in before the swept log groups
    # -- losing a swept log group is survivable, losing the pointers to the files the user consented
    # to attach is not.
    callbacks: tuple[_UploadCallback, ...] = ()
    uploader = get_attachments_uploader()
    if uploader is not None and description:
        description_uri, description_callback = uploader.prepare_description_upload(description)
        # A URI is only produced once an S3 bucket is configured, and that is the same condition
        # the upload itself needs -- so with no URI to reference there is nothing to schedule.
        if description_uri is not None:
            extra[BUG_REPORT_DESCRIPTION_EXTRA_KEY] = [description_uri]
            callbacks += (description_callback,)
    if uploader is not None and report_file_paths:
        # This report's own staged files, attached one-shot (see
        # prepare_report_file_uploads): they must appear on this event and
        # never on any other.
        report_uri_groups, report_callbacks = uploader.prepare_report_file_uploads(report_file_paths)
        for name, s3_uris in report_uri_groups.items():
            extra[f"{EXTRAS_UPLOADED_FILES_KEY}_{name}"] = list(s3_uris)
        callbacks += report_callbacks
    if report_file_uris:
        # Keys reserved before their bytes existed: nothing to upload from here, the collection that
        # is still running writes to those exact keys (see reserve_report_file_uploads). No uploader
        # is consulted -- reserving is what produced these uris.
        for name, reserved_uri in report_file_uris.items():
            extra[f"{EXTRAS_UPLOADED_FILES_KEY}_{name}"] = [reserved_uri]
    if uploader is not None and logs_folder is not None:
        # exception=None -> only log files are prepared (no synthesized traceback).
        s3_uri_groups, log_callbacks = uploader.collect_external_attachments(exception=None, logs_folder=logs_folder)
        for group_name, s3_uris in s3_uri_groups.items():
            extra[f"{EXTRAS_UPLOADED_FILES_KEY}_{group_name}"] = list(s3_uris)
        callbacks += log_callbacks

    if callbacks:
        handler = get_sentry_event_handler()
        if handler is not None:
            handler.schedule_callbacks(callbacks)
        else:
            # No loguru handler (e.g. Sentry initialized without the event handler): run the uploads
            # inline so the referenced S3 URLs resolve.
            for callback in callbacks:
                callback()

    return sentry_sdk.capture_event(event)
