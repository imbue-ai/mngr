import sys
from pathlib import Path
from typing import cast

import pytest
import sentry_sdk
from loguru import logger
from sentry_sdk import Client
from sentry_sdk import isolation_scope
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event
from sentry_sdk.types import Hint

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.utils.sentry.core import ErrorAttachmentsS3Uploader
from imbue.minds.utils.sentry.core import MANUALLY_SUBMITTED_TAG
from imbue.minds.utils.sentry.core import SENTRY_DSN_DEV
from imbue.minds.utils.sentry.core import SENTRY_DSN_PRODUCTION
from imbue.minds.utils.sentry.core import SENTRY_DSN_STAGING
from imbue.minds.utils.sentry.core import SentryDeployEnvironment
from imbue.minds.utils.sentry.core import _SENTRY_DSN_BY_ENVIRONMENT
from imbue.minds.utils.sentry.core import _before_send_wrapper
from imbue.minds.utils.sentry.core import _drop_interrupt_events
from imbue.minds.utils.sentry.core import _make_automatic_reporting_gate
from imbue.minds.utils.sentry.core import add_extra_info_hook
from imbue.minds.utils.sentry.core import resolve_sentry_environment
from imbue.minds.utils.sentry.core import submit_manual_bug_report
from imbue.minds.utils.sentry.loguru_handler import should_record_sentry_event


def test_from_minds_env_name_maps_production_and_staging() -> None:
    assert SentryDeployEnvironment.from_minds_env_name("production") is SentryDeployEnvironment.PRODUCTION
    assert SentryDeployEnvironment.from_minds_env_name("staging") is SentryDeployEnvironment.STAGING


@pytest.mark.parametrize("env_name", ["dev-josh-1", "ci-ephemeral", "", "Production", "STAGING", None])
def test_from_minds_env_name_defaults_to_development(env_name: str | None) -> None:
    assert SentryDeployEnvironment.from_minds_env_name(env_name) is SentryDeployEnvironment.DEVELOPMENT


@pytest.mark.parametrize(
    ("root_name", "expected"),
    [
        ("minds", SentryDeployEnvironment.PRODUCTION),
        ("minds-staging", SentryDeployEnvironment.STAGING),
        ("minds-dev-someone", SentryDeployEnvironment.DEVELOPMENT),
    ],
)
def test_resolve_sentry_environment_follows_root_name(
    monkeypatch: pytest.MonkeyPatch, root_name: str, expected: SentryDeployEnvironment
) -> None:
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, root_name)
    assert resolve_sentry_environment() is expected


def test_resolve_sentry_environment_defaults_to_development_when_unactivated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR, raising=False)
    assert resolve_sentry_environment() is SentryDeployEnvironment.DEVELOPMENT


def test_dsn_map_pairs_each_environment_with_a_distinct_dsn() -> None:
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.PRODUCTION] == SENTRY_DSN_PRODUCTION
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.STAGING] == SENTRY_DSN_STAGING
    assert _SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.DEVELOPMENT] == SENTRY_DSN_DEV
    assert len({SENTRY_DSN_PRODUCTION, SENTRY_DSN_STAGING, SENTRY_DSN_DEV}) == 3


def test_collect_external_attachments_classifies_flat_minds_log_layout(tmp_path: Path) -> None:
    # The minds logs dir is flat: a live `*.jsonl`, timestamp-suffixed rotated
    # `*.jsonl.<ts>` logs, and the Electron `*.log`. Each must land in its own
    # group, and the globs must not cross-match (e.g. `*.jsonl` must not pick up
    # the rotated files).
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    (logs_folder / "minds-events.jsonl").write_text("live\n")
    (logs_folder / "minds-events.jsonl.20250101120000123456").write_text("rotated\n")
    (logs_folder / "minds.log").write_text("electron\n")

    uploader = ErrorAttachmentsS3Uploader()
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=logs_folder)

    assert set(groups) == {"", "live_logs", "rotated_logs", "electron_logs"}
    assert len(groups["live_logs"]) == 1
    assert len(groups["rotated_logs"]) == 1
    assert len(groups["electron_logs"]) == 1
    # one callback per upload: traceback + the three log files.
    assert len(callbacks) == 4


def test_before_send_wrapper_logs_failure_locally_without_recursing_into_sentry() -> None:
    # When a before_send callback raises, the wrapper must surface the failure in the local app log
    # but must NOT let that log line become another Sentry event (which would re-enter this same
    # hook and recurse). The SentryEventHandler is modeled here by a sink guarded by the real filter.
    local_messages: list[str] = []
    sentry_messages: list[str] = []

    def boom(event: Event, hint: Hint) -> Event:
        raise ValueError("before_send boom")

    local_sink_id = logger.add(lambda message: local_messages.append(message.record["message"]), level=0)
    sentry_sink_id = logger.add(
        lambda message: sentry_messages.append(message.record["message"]),
        level=0,
        filter=should_record_sentry_event,
    )
    try:
        with pytest.raises(ValueError, match="before_send boom"):
            _before_send_wrapper({}, {}, [boom])
        # a normal error (no skip marker) must still flow to the Sentry-event sink: the filter only
        # suppresses records explicitly marked with _SKIP_SENTRY_EVENT_EXTRA_KEY.
        logger.error("ordinary error that should reach sentry")
    finally:
        logger.remove(local_sink_id)
        logger.remove(sentry_sink_id)

    assert any("before_send hook" in message for message in local_messages)
    assert not any("before_send hook" in message for message in sentry_messages)
    assert any("ordinary error" in message for message in sentry_messages)


def test_before_send_failure_reporting_does_not_recurse() -> None:
    # Reporting a before_send failure goes through capture_event, which re-runs the before_send chain.
    # A deterministically broken callback would otherwise recurse forever. The reentrancy guard must
    # bound this: the callback runs once for the original event and once for the minimal report event,
    # but the guard prevents the report from triggering yet another report.
    call_count = 0

    def always_broken(event: Event, hint: Hint) -> Event:
        nonlocal call_count
        call_count += 1
        raise ValueError("always broken before_send")

    class _NoOpTransport(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            pass

    def before_send(event: Event, hint: Hint) -> Event | None:
        return _before_send_wrapper(event, hint, [always_broken])

    client = Client(
        dsn="https://public@example.com/1",
        before_send=before_send,
        transport=_NoOpTransport(),
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    with isolation_scope() as scope:
        scope.set_client(client)
        # must terminate (no RecursionError) thanks to the guard.
        sentry_sdk.capture_event({"message": "trigger"})

    assert call_count == 2


def test_automatic_reporting_gate_drops_automatic_events_when_disabled() -> None:
    gate = _make_automatic_reporting_gate(lambda: False)
    assert gate({"message": "boom"}, {}) is None


def test_automatic_reporting_gate_passes_automatic_events_when_enabled() -> None:
    gate = _make_automatic_reporting_gate(lambda: True)
    event: Event = {"message": "boom"}
    assert gate(event, {}) is event


def test_automatic_reporting_gate_always_passes_manual_reports_even_when_disabled() -> None:
    # A manual bug report is an explicit user action and must be sent regardless of the automatic
    # reporting setting.
    gate = _make_automatic_reporting_gate(lambda: False)
    event: Event = {"message": "bug", "tags": {MANUALLY_SUBMITTED_TAG: "true"}}
    assert gate(event, {}) is event


def _hint_for_raised(exception: BaseException) -> Hint:
    """Build a before_send ``Hint`` carrying real ``exc_info`` for ``exception`` (raised to get a traceback)."""
    # Catch the exact type raised (covers KeyboardInterrupt / SystemExit, which are not Exception
    # subclasses) without catching the whole BaseException hierarchy.
    try:
        raise exception
    except type(exception):
        return cast(Hint, {"exc_info": sys.exc_info()})


def test_drop_interrupt_events_drops_keyboard_interrupt() -> None:
    # KeyboardInterrupt (Ctrl-C) reaches Sentry via the SDK's excepthook/threading integrations,
    # bypassing the loguru handler's own filter -- before_send must drop it since it is not a real fault.
    assert _drop_interrupt_events({"message": "x"}, _hint_for_raised(KeyboardInterrupt())) is None


@pytest.mark.parametrize("clean_exit", [SystemExit(), SystemExit(0), SystemExit(None), SystemExit(False)])
def test_drop_interrupt_events_drops_clean_system_exit(clean_exit: SystemExit) -> None:
    # A clean SystemExit (code None/0) is normal teardown, not an error.
    assert _drop_interrupt_events({"message": "x"}, _hint_for_raised(clean_exit)) is None


@pytest.mark.parametrize("fatal_exit", [SystemExit(1), SystemExit("boom")])
def test_drop_interrupt_events_keeps_nonzero_system_exit(fatal_exit: SystemExit) -> None:
    # A non-zero / message-bearing SystemExit is a genuine fatal-exit signal and must still report,
    # so a real error during shutdown is not silently swallowed.
    event: Event = {"message": "x"}
    assert _drop_interrupt_events(event, _hint_for_raised(fatal_exit)) is event


def test_drop_interrupt_events_keeps_ordinary_exception_and_eventless_hints() -> None:
    # Ordinary exceptions (the common case) and events without exc_info pass straight through.
    event: Event = {"message": "x"}
    assert _drop_interrupt_events(event, _hint_for_raised(ValueError("real error"))) is event
    assert _drop_interrupt_events(event, cast(Hint, {})) is event
    assert _drop_interrupt_events(event, cast(Hint, {"exc_info": (None, None, None)})) is event


def test_add_extra_info_hook_skips_attachments_when_log_inclusion_disabled() -> None:
    # With log inclusion off, no upload callbacks are prepared and no uploaded-files extras are added,
    # but the lightweight ``platform`` extra is still attached.
    event: Event = {"extra": {}}
    result_event, _hint, callbacks = add_extra_info_hook(event, {}, is_log_inclusion_enabled=lambda: False)
    assert callbacks == ()
    assert "platform" in result_event["extra"]
    assert not any(key.startswith("uploaded_files") for key in result_event["extra"])


def test_add_extra_info_hook_collects_traceback_when_log_inclusion_enabled() -> None:
    # With log inclusion on and no scope-configured log folder, the only attachment prepared is the
    # synthesized-traceback upload (one callback). Callbacks are partials, so nothing is uploaded here.
    event: Event = {"extra": {}}
    _result_event, _hint, callbacks = add_extra_info_hook(event, {}, is_log_inclusion_enabled=lambda: True)
    assert len(callbacks) == 1


def test_submit_manual_bug_report_sends_tagged_event_even_when_reporting_disabled() -> None:
    # A manual bug report is an explicit user action: it must reach Sentry even when the automatic
    # reporting gate is set to drop events, and it must carry the manual tag and the report payload.
    captured_events: list[Event] = []

    class _CapturingTransport(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            event = envelope.get_event()
            if event is not None:
                captured_events.append(event)

    gate = _make_automatic_reporting_gate(lambda: False)

    def before_send(event: Event, hint: Hint) -> Event | None:
        return _before_send_wrapper(event, hint, [gate])

    client = Client(
        dsn="https://public@example.com/1",
        before_send=before_send,
        transport=_CapturingTransport(),
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    with isolation_scope() as scope:
        scope.set_client(client)
        event_id = submit_manual_bug_report(
            title="[bug report] boom",
            report={"description": "boom", "remote_access_requested": False},
            include_logs=False,
            logs_folder=None,
        )
        client.flush()

    # The event id is returned so the user can quote it; capture_event yields a 32-char hex string.
    assert isinstance(event_id, str) and len(event_id) == 32
    assert len(captured_events) == 1
    event = captured_events[0]
    # ``Event`` types tags/extra loosely (object), so narrow before subscripting.
    tags = cast(dict, event["tags"])
    assert tags["manually_submitted"] == "true"
    extra = cast(dict, event["extra"])
    assert extra["bug_report"]["description"] == "boom"


def test_submit_manual_bug_report_returns_none_when_sentry_inactive() -> None:
    # With no active Sentry client (the default in tests), the submit is a no-op that returns None
    # (no event id) rather than raising.
    assert (
        submit_manual_bug_report(title="t", report={"description": "d"}, include_logs=False, logs_folder=None) is None
    )


def test_collect_external_attachments_without_logs_folder_only_uploads_traceback() -> None:
    uploader = ErrorAttachmentsS3Uploader()
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=None)

    assert set(groups) == {""}
    assert len(callbacks) == 1
