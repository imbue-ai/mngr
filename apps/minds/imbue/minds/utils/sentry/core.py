import functools
import gzip
import os
import re
import sys
import threading
import time
import traceback
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Hashable
from enum import StrEnum
from functools import cache
from functools import partial
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import MutableMapping
from typing import TypedDict
from typing import cast

import sentry_sdk
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
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.stdlib import StdlibIntegration
from sentry_sdk.types import Event
from sentry_sdk.types import Hint
from traceback_with_variables import Format

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.utils.sentry.loguru_handler import SENTRY_LOG_FORMAT
from imbue.minds.utils.sentry.loguru_handler import SentryBreadcrumbHandler
from imbue.minds.utils.sentry.loguru_handler import SentryEventHandler
from imbue.minds.utils.sentry.loguru_handler import SentryLoguruLoggingLevels
from imbue.minds.utils.sentry.loguru_handler import log_error_inside_sentry
from imbue.minds.utils.sentry.loguru_handler import should_record_sentry_event
from imbue.minds.utils.sentry.s3_uploader import EXTRAS_UPLOADED_FILES_KEY
from imbue.minds.utils.sentry.s3_uploader import get_s3_upload_key
from imbue.minds.utils.sentry.s3_uploader import get_s3_upload_url
from imbue.minds.utils.sentry.s3_uploader import setup_s3_uploads
from imbue.minds.utils.sentry.s3_uploader import upload_to_s3
from imbue.minds.utils.sentry.s3_uploader import upload_to_s3_with_key
from imbue.minds.utils.sentry.s3_uploader import wait_for_s3_uploads

# Minds writes all of its logs flat into a single logs directory (``~/.minds/logs``):
#   * ``minds-events.jsonl``       -- the live Python backend log (the loguru JSONL sink)
#   * ``minds-events.jsonl.<ts>``  -- rotated Python backend logs (timestamp-suffixed by make_jsonl_file_sink)
#   * ``minds.log``                -- the Electron main-process log
# None of these are gzip-compressed on disk, so every file is compressed on upload.
_LIVE_LOG_GLOB = "*.jsonl"
_ROTATED_LOG_GLOB = "*.jsonl.*"
_ELECTRON_LOG_GLOB = "*.log"
# suffix appended to the (gzip-compressed) S3 upload keys for log files
COMPRESSED_LOG_EXTENSION = "gz"


# sentry's size limits are annoyingly hard to evaluate before sending the event. we'll just try to be conservative.
# https://docs.sentry.io/concepts/data-management/size-limits/
# https://develop.sentry.dev/sdk/data-model/envelopes/#size-limits
MAX_SENTRY_ATTACHMENT_SIZE = 10 * 1024 * 1024


SENTRY_DSN_PRODUCTION = (
    "https://d8658891db0c1246864df82eefd74b6d@o4504335315501056.ingest.us.sentry.io/4511609235636224"
)
SENTRY_DSN_STAGING = "https://221f676a7e3c99733e85dc5c8dd6d6e2@o4504335315501056.ingest.us.sentry.io/4511609241862145"
SENTRY_DSN_DEV = "https://0a66e5894c00f701e3c1b7c2daae4650@o4504335315501056.ingest.us.sentry.io/4511609244811264"


class SentryDeployEnvironment(StrEnum):
    """Which Sentry project (and S3 bucket) a minds process reports to.

    Derived from the activated minds env (set by ``minds env activate``):
    ``production`` and ``staging`` each report to their own Sentry DSN and S3
    bucket; every other env (``dev-*``, ``ci-*``, or no activated env) reports
    to the shared dev Sentry project and uploads nothing to S3.
    """

    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"

    @classmethod
    def from_minds_env_name(cls, env_name: str | None) -> "SentryDeployEnvironment":
        """Map an activated minds env name to its Sentry environment.

        Only the exact names ``production`` and ``staging`` get their own
        targets; everything else (``dev-*``, ``ci-*``, or ``None`` when no env
        is activated) falls back to ``DEVELOPMENT``.
        """
        if env_name == cls.PRODUCTION.value:
            return cls.PRODUCTION
        if env_name == cls.STAGING.value:
            return cls.STAGING
        return cls.DEVELOPMENT


_SENTRY_DSN_BY_ENVIRONMENT: Mapping["SentryDeployEnvironment", str] = {
    SentryDeployEnvironment.PRODUCTION: SENTRY_DSN_PRODUCTION,
    SentryDeployEnvironment.STAGING: SENTRY_DSN_STAGING,
    SentryDeployEnvironment.DEVELOPMENT: SENTRY_DSN_DEV,
}


def resolve_sentry_environment() -> "SentryDeployEnvironment":
    """Select the Sentry environment from the activated minds env in the process env.

    ``production``/``staging`` map to their own targets; everything else (dev-*,
    ci-*, or no activated env) falls back to ``development``. Shared by the
    backend and the frontend so both report under the same environment.
    """
    activated_env_name = (
        env_name_from_root_name(resolve_minds_root_name()) if is_minds_root_name_set_to_active_env() else None
    )
    return SentryDeployEnvironment.from_minds_env_name(activated_env_name)


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
    You can see these at `https://generally-intelligent-e3.sentry.io/stats`, category "invalid".
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


# Tag set on events the user submits explicitly via the "report a bug" flow. Such events are always
# sent (an explicit user action), so the automatic-error gate below lets them through even when
# automatic error reporting is turned off.
MANUALLY_SUBMITTED_TAG = "manually_submitted"


class _AutomaticReportingGate(MutableModel):
    """before_send hook (a callable object, mirroring ``_SentryEventRateLimiter``) that drops automatic
    events while error reporting is disabled.

    ``is_error_reporting_enabled`` is read live on every event, so toggling the user setting takes
    effect without restarting. Events tagged ``MANUALLY_SUBMITTED_TAG`` always pass: a manual bug
    report is an explicit user action.
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
        # See this PR for the original motivation: https://gitlab.com/generally-intelligent/generally_intelligent/-/merge_requests/5789
        #
        # ``log_error_inside_sentry`` both records the failure in the local app log (so it is never lost)
        # and reports it to Sentry via a minimal event on a cleared scope. It is non-reentrant, so even
        # though reporting re-runs this same before_send chain, a deterministic before_send failure cannot
        # recurse: the nested report is dropped.
        log_error_inside_sentry(e, "Failure when processing event in before_send hook")
        # NOTE: this re-raise will get suppressed by Sentry and treated as if `before_send` returned `None`
        raise


def fixup_release_id(release_id: str) -> str:
    """
    For pre-release release candidate versions, Sentry requires the release ID to be in the semver format.

    E.g. "0.1.0rc1" should be converted to "0.1.0-rc.1".

    """
    return re.sub(r"(\d+\.\d+\.\d+)rc(\d+)", r"\1-rc.\2", release_id)


def setup_sentry(
    environment: SentryDeployEnvironment,
    release_id: str,
    git_commit_sha: str,
    log_folder: Path,
    is_error_reporting_enabled: Callable[[], bool],
    is_log_inclusion_enabled: Callable[[], bool],
) -> None:
    """Sets up the main Sentry instance for this process.

    This should be done *after* setting up normal loguru loggers, to ensure that sentry handling happens after normal logging.
    In case the sentry stuff hangs or something odd, we want to make sure to at least get regular log output.

    Sentry always initializes; what it actually *sends* is gated live by user setting:

    * ``is_error_reporting_enabled`` is read on every event (in a before_send hook). While it returns
      False, automatic events are dropped before they leave the process. Manually-submitted bug
      reports (tagged ``MANUALLY_SUBMITTED_TAG``) bypass this gate.
    * ``is_log_inclusion_enabled`` is read whenever attachments are collected; while it returns False,
      log/traceback attachments are skipped. This only matters in production/staging, where the S3
      bucket exists -- ``development`` never uploads attachments regardless.

    Both callables are read live, so toggling the corresponding user setting takes effect without an
    app restart.

    The ``environment`` selects the Sentry DSN (``production`` and ``staging`` each report to their
    own project; everything else to the shared dev project) and, for ``production``/``staging``,
    *which* S3 bucket attachments would go to.
    """
    if "SENTRY_DSN" in os.environ:
        # We pass ``dsn=`` explicitly below, so sentry_sdk ignores any SENTRY_DSN
        # in the environment. Warn rather than crash the backend: an end user may
        # have it set for unrelated reasons.
        logger.info("Ignoring SENTRY_DSN from the environment; minds selects its Sentry DSN by environment.")

    sentry_dsn = _SENTRY_DSN_BY_ENVIRONMENT[environment]

    # NOTE: the rate limiter object's lifetime is maintained by being captured in the
    #       closure of the before_send function.
    # Interrupt / clean-shutdown exceptions are dropped first (they are never real faults), then the
    # automatic-reporting gate drops events the user has opted out of, both before they consume a
    # rate-limiter slot.
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
        environment=environment.value,
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
        integrations=[
            FlaskIntegration(),
        ],
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

    # S3 attachment uploads are opt-in (off by default, even in production/staging),
    # because the uploaded log files + traceback-with-locals can carry
    # potentially-sensitive data. When enabled, the bucket follows the environment;
    # development never uploads regardless of the flag.
    # The S3 attachment uploader is initialized whenever the environment has a bucket
    # (production/staging). Whether logs/tracebacks are actually collected and uploaded is decided
    # live per-event by ``is_log_inclusion_enabled`` (in ``add_extra_info_hook``); development has no
    # bucket and never uploads.
    if environment is SentryDeployEnvironment.PRODUCTION:
        setup_s3_uploads(is_production=True)
        logger.info("Sentry S3 attachment uploader ready (production bucket)")
    elif environment is SentryDeployEnvironment.STAGING:
        setup_s3_uploads(is_production=False)
        logger.info("Sentry S3 attachment uploader ready (staging bucket)")
    else:
        logger.info("Sentry S3 attachment uploads disabled (environment={} has no bucket)", environment.value)

    # We deliberately do not call ``sentry_sdk.set_user`` (and keep
    # ``send_default_pii=False``) so error reports carry no user PII for now.

    # capture loguru errors/exceptions with a custom handler
    min_sentry_level: int = SentryLoguruLoggingLevels.LOW_PRIORITY.value
    handler = SentryEventHandler(
        level=min_sentry_level,
        add_extra_info_hook=partial(add_extra_info_hook, is_log_inclusion_enabled=is_log_inclusion_enabled),
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
    scope = get_current_scope()
    scope.set_context(
        _SENTRY_MINDS_CONTEXT_KEY,
        # need to cast to `dict` to make PyCharm happy
        cast(
            dict,
            SentryMindsConfigDict(
                log_folder_path=log_folder,
            ),
        ),
    )
    scope.set_tag("git_sha", git_commit_sha)
    logger.info("Sentry initialized with DSN: {}", sentry_dsn)
    logger.info("Sentry initialized with log folder: {}", log_folder)


_SENTRY_EVENT_HANDLER: SentryEventHandler | None = None


def register_sentry_event_handler(handler: SentryEventHandler) -> None:
    global _SENTRY_EVENT_HANDLER
    _SENTRY_EVENT_HANDLER = handler


def get_sentry_event_handler() -> SentryEventHandler | None:
    return _SENTRY_EVENT_HANDLER


# Keep this short: it runs on the desktop client's shutdown path, so a wedged or
# unreachable Sentry/S3 endpoint must not stall the user's app exit for long.
_SHUTDOWN_FLUSH_TIMEOUT_SECONDS: float = 3.0


def flush_sentry_on_shutdown(timeout: float = _SHUTDOWN_FLUSH_TIMEOUT_SECONDS) -> None:
    """Flush Sentry and its pending attachment uploads before the process exits.

    Called from the desktop client's teardown so errors captured late in the
    session are not lost. The order matters: first drain the loguru handler's
    add-extra-info callbacks (they enqueue the S3 attachment uploads), then wait
    for the S3 uploader's own pool to finish (so the URLs already referenced in
    captured events resolve), then flush the Sentry client so queued events are
    actually sent.

    The timeout is intentionally short so an unreachable Sentry/S3 endpoint can
    only briefly delay shutdown. Safe to call when Sentry was never set up (e.g.
    test factories that build the app without ``setup_sentry``): each step
    no-ops on an uninitialized client.
    """
    handler = get_sentry_event_handler()
    if handler is not None:
        handler.close()
    wait_for_s3_uploads(timeout=timeout, is_shutting_down=True)
    sentry_sdk.flush(timeout=timeout)


# sentry's size limits are annoyingly hard to evaluate before sending the event. we'll just try to be conservative.
# https://docs.sentry.io/concepts/data-management/size-limits/
# https://develop.sentry.dev/sdk/data-model/envelopes/#size-limits
MAX_SENTRY_ATTACHMENT_SIZE = 10 * 1024 * 1024
# sentry truncates any lists attached to the event["extra"] to this number
# Maciek could not find the documentation for that behavior
MAX_SENTRY_LIST_SIZE = 10

_SENTRY_MINDS_CONTEXT_KEY = "_config"


class SentryMindsConfigDict(TypedDict):
    log_folder_path: Path | None


def _get_config_from_scope() -> SentryMindsConfigDict:
    scope = get_current_scope()._contexts.get(_SENTRY_MINDS_CONTEXT_KEY, SentryMindsConfigDict(log_folder_path=None))
    # we only put SentryMindsConfigDict in _contexts, but regrettably as a third-party library we can't tell the checker that
    return cast(SentryMindsConfigDict, scope)


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


def _n_newest_files(files: Iterable[Path], n: int) -> Iterable[Path]:
    assert n > 0
    return sorted(files, key=lambda f: f.stat().st_mtime)[-n:]


# Callbacks returned by ``collect_external_attachments``: each is a pre-bound
# ``functools.partial`` that performs one S3 upload when invoked with no arguments.
_UploadCallback = Callable[[], None]


class ErrorAttachmentsS3Uploader(MutableModel):
    # FIXME: use a local instance of s3_uploader instead of the global one?

    # stores all previously uploaded rotated logs
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
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
        # Only cache immutable (rotated) files, whose contents never change, so a
        # later error report can reuse the same key instead of re-uploading them.
        # The live log is mutable and must be re-uploaded on every report.
        if uri is not None and immutable:
            with self._lock:
                # we assume that uri and key are in sync
                self._immutable_logs_keys[file_path] = key

    def collect_external_attachments(
        self, *, exception: BaseException | None, logs_folder: Path | None
    ) -> tuple[Mapping[str, Collection[str | None]], tuple[_UploadCallback, ...]]:
        """Prepares external uploads that will be attached to the error report.

        Returns external urls grouped by their logical names and the callbacks that need to be invoked which will
        actually perform the uploads to make those urls available.

        ``logs_folder`` is the minds logs directory (``~/.minds/logs``), whose layout is flat:
        a live ``*.jsonl`` Python backend log, timestamp-suffixed ``*.jsonl.<ts>`` rotated logs, and
        the Electron ``*.log``. All are gzip-compressed on upload.
        """
        uploads: dict[tuple[str, str], _UploadCallback | None] = {}

        if exception is not None:
            # this traceback is from the logger call site!
            key = get_s3_upload_key("logsite_traceback_with_vars", ".txt")
            uploads[("", key)] = partial(self._upload_traceback_cb, key=key, exception=exception)

        if logs_folder:
            # The live Python backend log (mutable -- re-upload on every report).
            for log_file in _n_newest_files(logs_folder.glob(_LIVE_LOG_GLOB), n=MAX_SENTRY_LIST_SIZE):
                key = get_s3_upload_key(log_file.name, f".{COMPRESSED_LOG_EXTENSION}")
                uploads[("live_logs", key)] = partial(self._upload_file_cb, key=key, file_path=log_file, compress=True)

            # Rotated Python backend logs (immutable -- upload once and reuse the cached key).
            for log_file in _n_newest_files(logs_folder.glob(_ROTATED_LOG_GLOB), n=1):
                with self._lock:
                    existing_key = self._immutable_logs_keys.get(log_file)

                if existing_key is not None:
                    logger.trace("Not uploading {} because it already exists under {}", log_file, existing_key)
                    uploads[("rotated_logs", existing_key)] = None
                else:
                    key = get_s3_upload_key(log_file.name, f".{COMPRESSED_LOG_EXTENSION}")
                    uploads[("rotated_logs", key)] = partial(
                        self._upload_file_cb, key=key, file_path=log_file, compress=True, immutable=True
                    )

            # The Electron main-process log.
            for log_file in _n_newest_files(logs_folder.glob(_ELECTRON_LOG_GLOB), n=MAX_SENTRY_LIST_SIZE):
                key = get_s3_upload_key(log_file.name, f".{COMPRESSED_LOG_EXTENSION}")
                uploads[("electron_logs", key)] = partial(
                    self._upload_file_cb,
                    key=key,
                    file_path=log_file,
                    compress=True,
                )

        grouped_uris: defaultdict[str, list[str | None]] = defaultdict(list)
        for group, key in uploads.keys():
            grouped_uris[group].append(get_s3_upload_url(key))

        callbacks = tuple(c for c in uploads.values() if c is not None)
        return grouped_uris, callbacks

    @staticmethod
    def _wait_for_all_uploads(timeout: float | None) -> bool | None:
        """Only to be used for testing, to avoid coupling tests with the global object"""
        return wait_for_s3_uploads(timeout=timeout, is_shutting_down=False)


_ATTACHMENTS_UPLOADER = ErrorAttachmentsS3Uploader()


def add_extra_info_hook(
    event: Event, hint: Hint, is_log_inclusion_enabled: Callable[[], bool]
) -> tuple[Event, Hint, tuple[_UploadCallback, ...]]:
    """The add_extra_info_hook gets called in the SentryEventHandler. This seems a little too early in the process for
    sending things to s3.

    Sentry may still decide to discard the issue and in that scenario, executing all the uploads now would just
    blackhole them.

    Log/traceback attachment collection is gated by ``is_log_inclusion_enabled`` (read live): while it
    returns False, no log or traceback uploads are prepared, so the event carries no attachments. The
    lightweight ``platform`` extra is always added regardless.
    """
    extra = cast(dict[str, Any], event["extra"])

    if is_log_inclusion_enabled():
        exception = sys.exception()
        if exception is None:
            try:
                raise Exception("this is an exception to get the current traceback")
            except Exception as e:
                exception = e

        s3_uri_groups, callbacks = _ATTACHMENTS_UPLOADER.collect_external_attachments(
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
    report: Mapping[str, Any],
    include_logs: bool,
    logs_folder: Path | None,
) -> str | None:
    """Synthesize and send a user-submitted bug report as a Sentry event.

    Unlike automatic error reporting, this is an explicit user action: the event is tagged
    ``MANUALLY_SUBMITTED_TAG`` so the automatic-reporting gate always lets it through, even when
    automatic error reporting is turned off. It is not tied to an exception -- ``title`` becomes the
    event message and ``report`` is attached as structured context.

    When ``include_logs`` is set and a ``logs_folder`` is given, recent log files are uploaded via the
    same S3-attachment mechanism as automatic errors (a no-op in environments without an S3 bucket).
    No traceback is collected (a manual report has no meaningful one).

    Returns the Sentry event id (a 32-char hex string the user can quote when following up), or None
    if Sentry is not active or the event was dropped before sending.
    """
    client = sentry_sdk.get_client()
    if not client.is_active():
        logger.info("Sentry is not active; manual bug report was not sent")
        return None

    # Build ``extra`` as a local dict (also referenced by the event) so log-attachment URLs can be
    # added without re-subscripting the loosely-typed Event TypedDict.
    extra: dict[str, Any] = {"bug_report": dict(report)}
    event: Event = {
        "message": title,
        "level": "info",
        "tags": {MANUALLY_SUBMITTED_TAG: "true"},
        "extra": extra,
    }

    if include_logs and logs_folder is not None:
        # exception=None -> only log files are prepared (no synthesized traceback).
        s3_uri_groups, callbacks = _ATTACHMENTS_UPLOADER.collect_external_attachments(
            exception=None, logs_folder=logs_folder
        )
        for group_name, s3_uris in s3_uri_groups.items():
            extra[f"{EXTRAS_UPLOADED_FILES_KEY}_{group_name}"] = list(s3_uris)
        handler = get_sentry_event_handler()
        if handler is not None:
            handler.schedule_callbacks(callbacks)
        else:
            # No loguru handler (e.g. Sentry initialized without the event handler): run the uploads
            # inline so the referenced S3 URLs resolve.
            for callback in callbacks:
                callback()

    return sentry_sdk.capture_event(event)
