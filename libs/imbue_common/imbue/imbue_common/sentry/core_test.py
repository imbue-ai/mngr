from pathlib import Path

import pytest
import sentry_sdk
from loguru import logger
from sentry_sdk import Client
from sentry_sdk import isolation_scope
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event
from sentry_sdk.types import Hint

from imbue.imbue_common.sentry.core import ErrorAttachmentsS3Uploader
from imbue.imbue_common.sentry.core import _before_send_wrapper
from imbue.imbue_common.sentry.core import fixup_release_id
from imbue.imbue_common.sentry.data_types import LogAttachmentGroup
from imbue.imbue_common.sentry.data_types import SENTRY_DSN_BY_ENVIRONMENT
from imbue.imbue_common.sentry.data_types import SENTRY_DSN_DEV
from imbue.imbue_common.sentry.data_types import SENTRY_DSN_PRODUCTION
from imbue.imbue_common.sentry.data_types import SENTRY_DSN_STAGING
from imbue.imbue_common.sentry.data_types import SentryDeployEnvironment
from imbue.imbue_common.sentry.loguru_handler import should_record_sentry_event

_LIVE_LOG_GROUP = LogAttachmentGroup(
    group_name="live_logs", glob="*.jsonl", max_file_count=10, is_compressed=True, is_immutable=False
)
_ROTATED_LOG_GROUP = LogAttachmentGroup(
    group_name="rotated_logs", glob="*.jsonl.*", max_file_count=1, is_compressed=True, is_immutable=True
)


def test_dsn_map_pairs_each_environment_with_a_distinct_dsn() -> None:
    assert SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.PRODUCTION] == SENTRY_DSN_PRODUCTION
    assert SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.STAGING] == SENTRY_DSN_STAGING
    assert SENTRY_DSN_BY_ENVIRONMENT[SentryDeployEnvironment.DEVELOPMENT] == SENTRY_DSN_DEV
    assert len({SENTRY_DSN_PRODUCTION, SENTRY_DSN_STAGING, SENTRY_DSN_DEV}) == 3


@pytest.mark.parametrize(
    ("release_id", "expected"),
    [("0.1.0rc1", "0.1.0-rc.1"), ("1.2.3", "1.2.3"), ("0.0.0+unknown", "0.0.0+unknown")],
)
def test_fixup_release_id_normalizes_release_candidates(release_id: str, expected: str) -> None:
    assert fixup_release_id(release_id) == expected


def test_collect_external_attachments_groups_logs_by_configured_glob(tmp_path: Path) -> None:
    # Each configured group's glob must land in its own group and not cross-match.
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    (logs_folder / "events.jsonl").write_text("live\n")
    (logs_folder / "events.jsonl.20250101120000123456").write_text("rotated\n")

    uploader = ErrorAttachmentsS3Uploader(log_attachment_groups=(_LIVE_LOG_GROUP, _ROTATED_LOG_GROUP))
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=logs_folder)

    assert set(groups) == {"", "live_logs", "rotated_logs"}
    assert len(groups["live_logs"]) == 1
    assert len(groups["rotated_logs"]) == 1
    # one callback per upload: traceback + the two log files (the immutable rotated
    # file is cached only after its first upload, so it still produces a callback here).
    assert len(callbacks) == 3


def test_collect_external_attachments_without_logs_folder_only_uploads_traceback() -> None:
    uploader = ErrorAttachmentsS3Uploader(log_attachment_groups=(_LIVE_LOG_GROUP,))
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=None)

    assert set(groups) == {""}
    assert len(callbacks) == 1


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
