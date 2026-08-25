import gzip
import io
import logging
import sys
import zipfile
from collections.abc import Callable
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import cast

import pytest
import sentry_sdk
from loguru import logger
from pydantic import Field
from sentry_sdk.integrations.logging import EventHandler
from sentry_sdk.integrations.logging import unignore_logger
from sentry_sdk.types import Event
from sentry_sdk.types import Hint

from imbue.imbue_common.sentry.core import BUG_REPORT_DESCRIPTION_EXTRA_KEY
from imbue.imbue_common.sentry.core import ErrorAttachmentsS3Uploader
from imbue.imbue_common.sentry.core import MANUALLY_SUBMITTED_TAG
from imbue.imbue_common.sentry.core import _before_send_wrapper
from imbue.imbue_common.sentry.core import _bug_report_description_bytes
from imbue.imbue_common.sentry.core import _drop_interrupt_events
from imbue.imbue_common.sentry.core import _make_automatic_reporting_gate
from imbue.imbue_common.sentry.core import _register_ignored_loggers
from imbue.imbue_common.sentry.core import add_extra_info_hook
from imbue.imbue_common.sentry.core import fixup_release_id
from imbue.imbue_common.sentry.core import get_or_create_anonymous_user_id
from imbue.imbue_common.sentry.core import submit_manual_bug_report
from imbue.imbue_common.sentry.data_types import LogAttachmentGroup
from imbue.imbue_common.sentry.loguru_handler import should_record_sentry_event
from imbue.imbue_common.sentry.s3_uploader import EXTRAS_UPLOADED_FILES_KEY
from imbue.imbue_common.sentry.testing import TEST_S3_BUCKET
from imbue.imbue_common.sentry.testing import capturing_sentry_client
from imbue.imbue_common.sentry.testing import recording_s3_bucket
from imbue.imbue_common.sentry.testing import registered_attachments_uploader

_LIVE_LOG_GROUP = LogAttachmentGroup(
    group_name="live_logs", glob="*.jsonl", max_file_count=10, is_compressed=True, is_immutable=False
)
_ROTATED_LOG_GROUP = LogAttachmentGroup(
    group_name="rotated_logs", glob="*.jsonl.*", max_file_count=1, is_compressed=True, is_immutable=True
)


@pytest.mark.parametrize(
    ("release_id", "expected"),
    [("0.1.0rc1", "0.1.0-rc.1"), ("1.2.3", "1.2.3"), ("0.0.0+unknown", "0.0.0+unknown")],
)
def test_fixup_release_id_normalizes_release_candidates(release_id: str, expected: str) -> None:
    assert fixup_release_id(release_id) == expected


def test_get_or_create_anonymous_user_id_creates_and_persists_a_hex_id(tmp_path: Path) -> None:
    id_file_path = tmp_path / "anonymous_user_id"
    user_id = get_or_create_anonymous_user_id(id_file_path)
    # A 32-char lowercase hex string (uuid4().hex), persisted verbatim to the file.
    assert len(user_id) == 32 and all(char in "0123456789abcdef" for char in user_id)
    assert id_file_path.read_text() == user_id


def test_get_or_create_anonymous_user_id_is_stable_across_calls(tmp_path: Path) -> None:
    id_file_path = tmp_path / "anonymous_user_id"
    first = get_or_create_anonymous_user_id(id_file_path)
    second = get_or_create_anonymous_user_id(id_file_path)
    assert first == second


def test_get_or_create_anonymous_user_id_regenerates_a_malformed_file(tmp_path: Path) -> None:
    id_file_path = tmp_path / "anonymous_user_id"
    id_file_path.write_text("not-a-valid-id")
    user_id = get_or_create_anonymous_user_id(id_file_path)
    assert len(user_id) == 32 and all(char in "0123456789abcdef" for char in user_id)
    assert id_file_path.read_text() == user_id


def test_get_or_create_anonymous_user_id_creates_parent_directory(tmp_path: Path) -> None:
    id_file_path = tmp_path / "nested" / "dir" / "anonymous_user_id"
    user_id = get_or_create_anonymous_user_id(id_file_path)
    assert id_file_path.read_text() == user_id


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


def test_collect_external_attachments_globs_group_base_dir_over_logs_folder(tmp_path: Path) -> None:
    # A group with a base_dir sweeps that directory (even with no process log folder);
    # groups without one keep sweeping the log folder. A missing base_dir matches nothing.
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    (logs_folder / "events.jsonl").write_text("live\n")
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "daemon.log").write_text("daemon\n")
    external_group = LogAttachmentGroup(
        group_name="daemon_logs",
        glob="*.log",
        max_file_count=10,
        is_compressed=True,
        is_immutable=False,
        base_dir=external_dir,
    )
    missing_dir_group = LogAttachmentGroup(
        group_name="absent_logs",
        glob="*.log",
        max_file_count=10,
        is_compressed=True,
        is_immutable=False,
        base_dir=tmp_path / "does-not-exist",
    )

    uploader = ErrorAttachmentsS3Uploader(log_attachment_groups=(_LIVE_LOG_GROUP, external_group, missing_dir_group))
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=logs_folder)

    assert set(groups) == {"", "live_logs", "daemon_logs"}
    assert len(groups["daemon_logs"]) == 1
    assert len(callbacks) == 3

    # base_dir groups are swept even when the process has no log folder at all.
    try:
        raise ValueError("boom again")
    except ValueError as exception:
        groups, _callbacks = uploader.collect_external_attachments(exception=exception, logs_folder=None)
    assert set(groups) == {"", "daemon_logs"}


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

    def before_send(event: Event, hint: Hint) -> Event | None:
        return _before_send_wrapper(event, hint, [always_broken])

    with capturing_sentry_client(before_send=before_send):
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


def test_add_extra_info_hook_skips_attachments_when_reporting_disabled() -> None:
    # With reporting off (the event will be dropped anyway), no upload callbacks are prepared and no
    # uploaded-files extras are added, but the lightweight ``platform`` extra is still attached.
    event: Event = {"extra": {}}
    with registered_attachments_uploader(ErrorAttachmentsS3Uploader()):
        result_event, _hint, callbacks = add_extra_info_hook(event, {}, is_error_reporting_enabled=lambda: False)
    assert callbacks == ()
    assert "platform" in result_event["extra"]
    assert not any(key.startswith("uploaded_files") for key in result_event["extra"])


def test_add_extra_info_hook_collects_traceback_when_reporting_enabled() -> None:
    # With reporting on and no scope-configured log folder, the only attachment prepared is the
    # synthesized-traceback upload (one callback). Callbacks are partials, so nothing is uploaded here.
    event: Event = {"extra": {}}
    with registered_attachments_uploader(ErrorAttachmentsS3Uploader()):
        _result_event, _hint, callbacks = add_extra_info_hook(event, {}, is_error_reporting_enabled=lambda: True)
    assert len(callbacks) == 1


def test_scope_user_id_is_attached_to_events_even_with_send_default_pii_off() -> None:
    # The feature relies on an explicitly-set anonymous user id being sent to Sentry even though
    # send_default_pii is False (which only suppresses auto-collected PII like IP addresses). Verify
    # the id set on the scope (exactly what setup_sentry does) lands on a captured event's user.
    with capturing_sentry_client() as captured_events:
        # The precondition this test is named for lives in the shared client helper, so pin it here:
        # a change there must not quietly turn this into a test of nothing.
        assert sentry_sdk.get_client().options["send_default_pii"] is False
        sentry_sdk.set_user({"id": "0123456789abcdef0123456789abcdef"})
        sentry_sdk.capture_event({"message": "boom"})

    assert len(captured_events) == 1
    user = cast(dict, captured_events[0]["user"])
    assert user["id"] == "0123456789abcdef0123456789abcdef"


def test_submit_manual_bug_report_sends_tagged_event_even_when_reporting_disabled() -> None:
    # A manual bug report is an explicit user action: it must reach Sentry even when the automatic
    # reporting gate is set to drop events, and it must carry the manual tag and the report payload.
    gate = _make_automatic_reporting_gate(lambda: False)

    def before_send(event: Event, hint: Hint) -> Event | None:
        return _before_send_wrapper(event, hint, [gate])

    with capturing_sentry_client(before_send=before_send) as captured_events:
        event_id = submit_manual_bug_report(
            title="[bug report] boom",
            description="boom",
            report={"description": "boom", "remote_access_requested": False},
            logs_folder=None,
        )

    # The event id is returned so the user can quote it; capture_event yields a 32-char hex string.
    assert isinstance(event_id, str) and len(event_id) == 32
    assert len(captured_events) == 1
    event = captured_events[0]
    # ``Event`` types tags/extra loosely (object), so narrow before subscripting.
    tags = cast(dict, event["tags"])
    assert tags["manually_submitted"] == "true"
    extra = cast(dict, event["extra"])
    assert extra["bug_report"]["description"] == "boom"


_FAKE_DESCRIPTION_URI = "s3://test-bucket/bug_report_description_2024-01-01T00-00-00_deadbeef.txt"
# Ordinary prose that trips Sentry's built-in password pattern (it is a set of plain substrings, so
# "secrets" and "authored" both match), which is what destroys the inline copy of a report body.
_SCRUBBER_TRIPPING_DESCRIPTION = "restic secrets were not authored correctly"


class _RecordingUploader(ErrorAttachmentsS3Uploader):
    """Stands in for a configured S3 bucket: records what was handed to it and yields a canned URI."""

    prepared_descriptions: list[str] = Field(default_factory=list)
    uploaded_descriptions: list[str] = Field(default_factory=list)
    uploaded_file_paths: list[Path] = Field(default_factory=list)

    def prepare_description_upload(self, description: str) -> tuple[str | None, Callable[[], None]]:
        self.prepared_descriptions.append(description)
        return _FAKE_DESCRIPTION_URI, partial(self.uploaded_descriptions.append, description)

    def _upload_file_cb(self, key: str, file_path: Path, compress: bool = False, immutable: bool = False) -> None:
        self.uploaded_file_paths.append(file_path)


def test_submit_manual_bug_report_copies_the_description_out_of_band_without_a_logs_folder() -> None:
    # The whole point of the out-of-band copy is that it survives the scrubber, so it has to be made
    # for *every* report -- not only for the reports that happen to carry log attachments. Pin that by
    # submitting with no logs folder at all and requiring the copy to still be prepared, uploaded, and
    # referenced from the event. (Sentry is what scrubs, so the inline copy here is untouched; what
    # this guards is that the second copy exists to fall back on.)
    uploader = _RecordingUploader()
    with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
        submit_manual_bug_report(
            title="[bug report] boom",
            description=_SCRUBBER_TRIPPING_DESCRIPTION,
            report={"description": _SCRUBBER_TRIPPING_DESCRIPTION},
            logs_folder=None,
        )

    # Verbatim: the out-of-band copy is worthless if it is trimmed or rewritten on the way out.
    assert uploader.prepared_descriptions == [_SCRUBBER_TRIPPING_DESCRIPTION]
    # No loguru handler is registered under test, so submit_manual_bug_report runs the upload inline
    # -- meaning a scheduled-but-never-run callback would show up here as an empty list.
    assert uploader.uploaded_descriptions == [_SCRUBBER_TRIPPING_DESCRIPTION]
    assert len(captured_events) == 1
    extra = cast(dict, captured_events[0]["extra"])
    assert extra[BUG_REPORT_DESCRIPTION_EXTRA_KEY] == [_FAKE_DESCRIPTION_URI]
    # The inline copy is still sent alongside it: it survives the scrubber for most reports and is
    # the convenient one to read.
    assert extra["bug_report"]["description"] == _SCRUBBER_TRIPPING_DESCRIPTION


def test_submit_manual_bug_report_attaches_report_files_one_shot(tmp_path: Path) -> None:
    """Report files ride only on the report they were staged for.

    They are named by exact path rather than matched by the process-global
    groups, so the event carries their extras keys while a later error event's
    sweep -- which sees the same folder -- picks up nothing. Both halves are
    pinned: the report references and uploads the files, and a subsequent
    error-path collection over the same folder does not.
    """
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    staged = logs_folder / "bug-report-transcript.log"
    staged.write_text('{"type": "user_message"}\n')
    uploader = _RecordingUploader()
    with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
        submit_manual_bug_report(
            title="[bug report] boom",
            description="something broke",
            report={"description": "something broke"},
            logs_folder=None,
            report_file_paths={"bug_report_transcript": staged},
        )

    assert uploader.uploaded_file_paths == [staged]
    assert len(captured_events) == 1
    extra = cast(dict, captured_events[0]["extra"])
    assert f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_transcript" in extra

    # The same folder, swept by the error path with no groups configured for
    # these names, must not touch the staged file.
    try:
        raise ValueError("boom")
    except ValueError as exception:
        groups, _ = uploader.collect_external_attachments(exception=exception, logs_folder=logs_folder)
    assert "bug_report_transcript" not in groups


def test_submit_manual_bug_report_skips_missing_report_files(tmp_path: Path) -> None:
    """A path whose file vanished contributes neither an extras key nor an upload."""
    uploader = _RecordingUploader()
    with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
        submit_manual_bug_report(
            title="[bug report] boom",
            description="something broke",
            report={"description": "something broke"},
            logs_folder=None,
            report_file_paths={"bug_report_transcript": tmp_path / "never-written.log"},
        )

    assert uploader.uploaded_file_paths == []
    extra = cast(dict, captured_events[0]["extra"])
    assert f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_transcript" not in extra


# One member per recent chat, exactly as the in-container collector names them.
_TRANSCRIPT_ARCHIVE_MEMBER_NAME = "agent-1-claude.jsonl"


def _write_chat_transcript_archive(path: Path) -> bytes:
    """Write a chat-transcript zip like the collector stages, returning its bytes verbatim."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_TRANSCRIPT_ARCHIVE_MEMBER_NAME, '{"type": "user_message"}\n')
    return path.read_bytes()


def test_submit_manual_bug_report_publishes_reserved_uris_before_the_files_exist() -> None:
    """A reserved uri reaches the event with nothing uploaded yet.

    This is what lets the submit return an event id immediately while collection (tens of seconds of
    it) is still running: the key is minted locally, so the uri can be published first and the bytes
    written to it afterwards.
    """
    with recording_s3_bucket() as uploads:
        uploader = ErrorAttachmentsS3Uploader()
        reservations = uploader.reserve_report_file_uploads(
            {"bug_report_transcript": ".zip", "bug_report_logs": ".log"}
        )
        assert uploads == []

        with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
            submit_manual_bug_report(
                title="[bug report] boom",
                description="",
                report={"description": ""},
                logs_folder=None,
                report_file_uris={name: uri for name, (uri, _key) in reservations.items()},
            )

    assert len(captured_events) == 1
    extra = cast(dict, captured_events[0]["extra"])
    for name, (uri, key) in reservations.items():
        assert uri == f"s3://{TEST_S3_BUCKET}/{key}"
        assert extra[f"{EXTRAS_UPLOADED_FILES_KEY}_{name}"] == [uri]
    # The event is captured with its attachments still unwritten -- that is the entire point.
    assert uploads == []


def test_submit_manual_bug_report_keeps_every_attachment_pointer_on_a_full_size_report(tmp_path: Path) -> None:
    """Every ``uploaded_files_*`` extra survives to the delivered event at real-report size.

    A real minds report carries its ``bug_report`` payload, the description uri, ten swept log
    groups, and three reserved report-file uris -- 15 extra keys. Sentry's client-side serializer
    keeps only the first ``MAX_DATABAG_BREADTH`` keys of the extra dict (recording a ``_meta``
    marker and dropping the rest), and with the SDK default of 10 the keys inserted last -- the
    pointers to the report's own attachments -- were exactly what fell off.
    ``raise_sentry_databag_breadth`` (applied by ``setup_sentry`` and mirrored by the capturing
    client) lifts the cap; this pins that a full-size report reaches the wire whole.
    """
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    swept_groups: list[LogAttachmentGroup] = []
    for group_idx in range(10):
        log_name = f"service_{group_idx}.log"
        (logs_folder / log_name).write_text("log line\n")
        swept_groups.append(
            LogAttachmentGroup(
                group_name=f"service_{group_idx}",
                glob=log_name,
                max_file_count=1,
                is_compressed=True,
                is_immutable=False,
            )
        )

    with recording_s3_bucket():
        uploader = ErrorAttachmentsS3Uploader(log_attachment_groups=tuple(swept_groups))
        reservations = uploader.reserve_report_file_uploads(
            {"bug_report_workspace": ".zip", "bug_report_console": ".log"}
        )
        status_uri, _status_key = uploader.reserve_text_upload("bug_report_attachment_status")
        with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
            submit_manual_bug_report(
                title="[bug report] boom",
                description="something broke",
                report={"description": "something broke"},
                logs_folder=logs_folder,
                report_file_uris={
                    **{name: uri for name, (uri, _key) in reservations.items()},
                    "bug_report_attachment_status": status_uri,
                },
            )

    assert len(captured_events) == 1
    event = captured_events[0]
    extra = cast(dict, event["extra"])
    expected_keys = {"bug_report", BUG_REPORT_DESCRIPTION_EXTRA_KEY}
    expected_keys |= {f"{EXTRAS_UPLOADED_FILES_KEY}_service_{group_idx}" for group_idx in range(10)}
    expected_keys |= {
        f"{EXTRAS_UPLOADED_FILES_KEY}_{name}"
        for name in ("bug_report_workspace", "bug_report_console", "bug_report_attachment_status")
    }
    assert expected_keys <= set(extra)
    # The serializer records every trim in the event's ``_meta``; a whole report must produce none.
    assert "_meta" not in event


def test_submit_manual_bug_report_inserts_report_pointers_before_swept_log_groups(tmp_path: Path) -> None:
    """The report's own attachment extras precede the swept log-group extras in insertion order.

    Anything that trims an extra dict keeps the first entries and drops the last, so insertion order
    is priority order: should truncation ever return (say, an SDK upgrade quietly reverting the
    breadth raise), a swept log group is what falls off -- never the description or the pointers to
    the files the user consented to attach.
    """
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    (logs_folder / "events.jsonl").write_text("live\n")
    staged = tmp_path / "bug-report-transcript.zip"
    _write_chat_transcript_archive(staged)

    with recording_s3_bucket():
        uploader = ErrorAttachmentsS3Uploader(log_attachment_groups=(_LIVE_LOG_GROUP,))
        reserved_uri, _key = uploader.reserve_report_file_uploads({"bug_report_console": ".log"})["bug_report_console"]
        with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
            submit_manual_bug_report(
                title="[bug report] boom",
                description="something broke",
                report={"description": "something broke"},
                logs_folder=logs_folder,
                report_file_paths={"bug_report_transcript": staged},
                report_file_uris={"bug_report_console": reserved_uri},
            )

    extra_keys = list(cast(dict, captured_events[0]["extra"]))
    own_pointer_keys = [
        BUG_REPORT_DESCRIPTION_EXTRA_KEY,
        f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_transcript",
        f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_console",
    ]
    swept_group_key = f"{EXTRAS_UPLOADED_FILES_KEY}_{_LIVE_LOG_GROUP.group_name}"
    assert max(extra_keys.index(key) for key in own_pointer_keys) < extra_keys.index(swept_group_key)


def test_reserved_and_staged_attachments_produce_identically_shaped_extras(tmp_path: Path) -> None:
    # Whoever reads the event cannot tell (and must not need to tell) whether an attachment was
    # already staged at submit time or was still being collected, so both paths must write the same
    # ``uploaded_files_<name>`` shape: a one-element list holding the uri.
    staged = tmp_path / "bug-report-transcript.log"
    staged.write_text('{"type": "user_message"}\n')
    extras_key = f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_transcript"

    with recording_s3_bucket():
        uploader = ErrorAttachmentsS3Uploader()
        with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
            submit_manual_bug_report(
                title="[bug report] boom",
                description="",
                report={},
                logs_folder=None,
                report_file_paths={"bug_report_transcript": staged},
            )
            reserved_uri, _key = uploader.reserve_report_file_uploads({"bug_report_transcript": ".log"})[
                "bug_report_transcript"
            ]
            submit_manual_bug_report(
                title="[bug report] boom",
                description="",
                report={},
                logs_folder=None,
                report_file_uris={"bug_report_transcript": reserved_uri},
            )

    staged_extra = cast(dict, captured_events[0]["extra"])[extras_key]
    reserved_extra = cast(dict, captured_events[1]["extra"])[extras_key]
    assert len(staged_extra) == 1 and len(reserved_extra) == 1
    assert staged_extra[0].startswith(f"s3://{TEST_S3_BUCKET}/bug-report-transcript.log_")
    assert reserved_extra[0].startswith(f"s3://{TEST_S3_BUCKET}/bug_report_transcript_")


def test_upload_reserved_report_file_writes_gzipped_bytes_to_the_key_its_uri_named(tmp_path: Path) -> None:
    # The reservation's uri is on an event that was already sent, so the later upload has exactly one
    # chance to hit the object it named: a different key (or uncompressed bytes under a ``.gz`` uri)
    # leaves a reader following a link to nothing, with nothing to notice it.
    staged = tmp_path / "bug-report-abc-workspace-logs.log"
    contents = b"staged workspace logs\n"
    staged.write_bytes(contents)

    with recording_s3_bucket() as uploads:
        uploader = ErrorAttachmentsS3Uploader()
        uri, key = uploader.reserve_report_file_uploads({"bug_report_logs": ".log"})["bug_report_logs"]
        uploader.upload_reserved_report_file(key=key, file_path=staged)

    assert key.endswith(".gz")
    assert len(uploads) == 1
    uploaded_key, uploaded_contents = uploads[0]
    assert uploaded_key == key
    assert uri == f"s3://{TEST_S3_BUCKET}/{uploaded_key}"
    assert gzip.decompress(uploaded_contents) == contents


def test_upload_reserved_report_file_writes_a_staged_archive_verbatim_to_its_zip_key(tmp_path: Path) -> None:
    # The chat-transcript archive is reserved from its suffix alone, before it exists, and the uri that
    # names a ``.zip`` is already out on a sent event. So the object there has to *be* the archive:
    # gzipping it would hand whoever follows that uri something that is not the zip the key claimed,
    # and no second chance to notice.
    staged = tmp_path / "bug-report-abc-transcript.zip"
    archive_bytes = _write_chat_transcript_archive(staged)

    with recording_s3_bucket() as uploads:
        uploader = ErrorAttachmentsS3Uploader()
        uri, key = uploader.reserve_report_file_uploads({"bug_report_transcript": ".zip"})["bug_report_transcript"]
        uploader.upload_reserved_report_file(key=key, file_path=staged)

    assert key.endswith(".zip")
    assert len(uploads) == 1
    uploaded_key, uploaded_contents = uploads[0]
    assert uploaded_key == key
    assert uri == f"s3://{TEST_S3_BUCKET}/{uploaded_key}"
    assert uploaded_contents == archive_bytes
    with zipfile.ZipFile(io.BytesIO(uploaded_contents)) as archive:
        assert archive.namelist() == [_TRANSCRIPT_ARCHIVE_MEMBER_NAME]


def test_prepare_report_file_uploads_gzips_a_staged_log_under_a_gz_key(tmp_path: Path) -> None:
    # The one-shot path reads the key suffix and the compression off the staged file's own name, so an
    # ordinary log keeps exactly the arrangement it has always had: gzip bytes under a ``.gz`` key.
    staged = tmp_path / "bug-report-abc-workspace-logs.log"
    contents = b"staged workspace logs\n"
    staged.write_bytes(contents)

    with recording_s3_bucket() as uploads:
        uris, callbacks = ErrorAttachmentsS3Uploader().prepare_report_file_uploads(
            {"bug_report_workspace_logs": staged}
        )
        for callback in callbacks:
            callback()

    assert len(uploads) == 1
    uploaded_key, uploaded_contents = uploads[0]
    assert uploaded_key.endswith(".gz")
    assert list(uris["bug_report_workspace_logs"]) == [f"s3://{TEST_S3_BUCKET}/{uploaded_key}"]
    assert gzip.decompress(uploaded_contents) == contents


def test_prepare_report_file_uploads_writes_a_staged_archive_verbatim_under_a_zip_key(tmp_path: Path) -> None:
    # Same rule on the fast path (the prefetch already finished, so the archive exists at submit time):
    # a reader who follows either path's uri must reach the archive itself, not a gzip wrapped around
    # one -- so the key keeps ``.zip`` and the bytes are stored untouched.
    staged = tmp_path / "bug-report-abc-transcript.zip"
    archive_bytes = _write_chat_transcript_archive(staged)

    with recording_s3_bucket() as uploads:
        uris, callbacks = ErrorAttachmentsS3Uploader().prepare_report_file_uploads({"bug_report_transcript": staged})
        for callback in callbacks:
            callback()

    assert len(uploads) == 1
    uploaded_key, uploaded_contents = uploads[0]
    assert uploaded_key.endswith(".zip")
    assert list(uris["bug_report_transcript"]) == [f"s3://{TEST_S3_BUCKET}/{uploaded_key}"]
    assert uploaded_contents == archive_bytes
    with zipfile.ZipFile(io.BytesIO(uploaded_contents)) as archive:
        assert archive.namelist() == [_TRANSCRIPT_ARCHIVE_MEMBER_NAME]


def test_prepare_report_file_uploads_does_not_re_gzip_a_file_already_gzipped_on_disk(tmp_path: Path) -> None:
    # The same rule the ``*.gz`` log attachment groups are declared with (``is_compressed=False``): a
    # file that arrives gzipped is stored as it is, so its ``.gz`` uri is one gunzip from the log.
    staged = tmp_path / "minds.log.20250101.gz"
    contents = b"rotated backend log\n"
    staged.write_bytes(gzip.compress(contents))

    with recording_s3_bucket() as uploads:
        _uris, callbacks = ErrorAttachmentsS3Uploader().prepare_report_file_uploads({"bug_report_logs": staged})
        for callback in callbacks:
            callback()

    assert len(uploads) == 1
    uploaded_key, uploaded_contents = uploads[0]
    assert uploaded_key.endswith(".gz")
    assert uploaded_contents == staged.read_bytes()
    assert gzip.decompress(uploaded_contents) == contents


def test_reserve_report_file_uploads_refuses_bytes_that_are_already_gzipped() -> None:
    # A ``.gz`` key is the one this uploader compresses into, so reserving one for bytes that are
    # already gzip would leave the upload -- which has only the key to go on -- no way to avoid
    # wrapping them a second time, under a key claiming a single layer.
    with pytest.raises(AssertionError, match="already-gzipped"):
        ErrorAttachmentsS3Uploader().reserve_report_file_uploads({"bug_report_logs": ".gz"})


def test_upload_reserved_report_file_tolerates_a_file_that_never_materialized(tmp_path: Path) -> None:
    # An attachment dropped for secrets (or one whose collection failed) never arrives at its reserved
    # key. That is expected, not an error: it must be recorded in the log and leave the object absent,
    # because raising would take the report's other uploads down with it.
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level=0)
    try:
        with recording_s3_bucket() as uploads:
            uploader = ErrorAttachmentsS3Uploader()
            _uri, key = uploader.reserve_report_file_uploads({"bug_report_logs": ".log"})["bug_report_logs"]
            uploader.upload_reserved_report_file(key=key, file_path=tmp_path / "never-written.log")
    finally:
        logger.remove(sink_id)

    assert uploads == []
    assert any("never materialized" in message for message in messages)


def test_upload_reserved_text_is_readable_straight_from_its_uri() -> None:
    # The status document exists to be read by whoever opens the Sentry event, so it is uploaded
    # verbatim and uncompressed under the key whose uri the event carries.
    status = "bug_report_logs: attached\nbug_report_transcript: omitted (collection failed)\n"
    with recording_s3_bucket() as uploads:
        uploader = ErrorAttachmentsS3Uploader()
        uri, key = uploader.reserve_text_upload("bug_report_attachment_status")
        uploader.upload_reserved_text(key=key, text=status)

    assert len(uploads) == 1
    uploaded_key, uploaded_contents = uploads[0]
    assert uploaded_key == key and uploaded_key.endswith(".txt")
    assert uri == f"s3://{TEST_S3_BUCKET}/{uploaded_key}"
    assert uploaded_contents == status.encode()


def test_submit_manual_bug_report_copies_the_description_alongside_the_log_attachments(tmp_path: Path) -> None:
    # Description copy and log attachments are prepared from the same uploader and uploaded through the
    # same set of callbacks, so the report that carries both is the one where they can displace each
    # other. Require every piece of evidence to survive: both uploads run, and the event references
    # both the description and the log group.
    logs_folder = tmp_path / "logs"
    logs_folder.mkdir()
    live_log = logs_folder / "events.jsonl"
    live_log.write_text("live\n")

    uploader = _RecordingUploader(log_attachment_groups=(_LIVE_LOG_GROUP,))
    with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
        submit_manual_bug_report(
            title="[bug report] boom",
            description=_SCRUBBER_TRIPPING_DESCRIPTION,
            report={"description": _SCRUBBER_TRIPPING_DESCRIPTION},
            logs_folder=logs_folder,
        )

    assert uploader.uploaded_descriptions == [_SCRUBBER_TRIPPING_DESCRIPTION]
    assert uploader.uploaded_file_paths == [live_log]
    assert len(captured_events) == 1
    extra = cast(dict, captured_events[0]["extra"])
    assert extra[BUG_REPORT_DESCRIPTION_EXTRA_KEY] == [_FAKE_DESCRIPTION_URI]
    # No bucket is configured under test, so the log group's uri is None; what matters here is that the
    # group still reaches the event (its uris resolve wherever a bucket exists).
    assert f"{EXTRAS_UPLOADED_FILES_KEY}_{_LIVE_LOG_GROUP.group_name}" in extra


def test_submit_manual_bug_report_gives_every_report_its_own_issue() -> None:
    # Sentry groups these by stack trace, which is the submitting code path and so identical for every
    # report -- collapsing all of them into one catch-all issue where assign/resolve/comment cannot
    # address a single report. Two reports sharing a title (the likely near-duplicate case) must still
    # land in separate issues, so fingerprint on identical input and require the fingerprints to differ.
    with capturing_sentry_client() as captured_events:
        for _ in range(2):
            submit_manual_bug_report(
                title="[bug report] boom", description="boom", report={"description": "boom"}, logs_folder=None
            )

    fingerprints = [cast(list, event["fingerprint"]) for event in captured_events]
    assert len(fingerprints) == 2
    assert fingerprints[0] != fingerprints[1]


def test_bug_report_description_bytes_survives_a_lone_surrogate() -> None:
    # A lone surrogate reaches here from JSON (json.loads accepts "\ud800"), and plain utf-8 refuses
    # to encode one. The encode runs inside the upload callback, so raising costs exactly the copy the
    # upload exists to make: swallowed on the handler's executor, or -- with no handler registered --
    # aborting the submit before the event is captured. It must escape, not raise, and not flatten to
    # "?" (which is indistinguishable from a "?" the user actually typed).
    assert _bug_report_description_bytes("before \ud800 after") == b"before \\ud800 after"


def test_prepare_description_upload_uploads_to_the_object_its_uri_names() -> None:
    # The URI is what goes on the event and the key is what the callback writes to, and the two are
    # only useful together: a URI derived from a different key than the upload uses points at an
    # object nobody ever wrote, with nothing to notice it -- the event still looks complete, and the
    # copy is only ever read once the inline one has already come back "[Filtered]". So run the real
    # method against a configured bucket and require the URI to name the object that was written.
    with recording_s3_bucket() as uploads:
        uri, upload_description = ErrorAttachmentsS3Uploader().prepare_description_upload(
            _SCRUBBER_TRIPPING_DESCRIPTION
        )
        upload_description()

    assert len(uploads) == 1
    key, contents = uploads[0]
    assert uri == f"s3://{TEST_S3_BUCKET}/{key}"
    # And the callback is what applies the encoding, so it cannot be dropped from this path unnoticed.
    assert contents == _bug_report_description_bytes(_SCRUBBER_TRIPPING_DESCRIPTION)


def test_prepare_description_upload_is_a_no_op_without_a_configured_bucket() -> None:
    # Environments with no S3 bucket (minds' `development`) must degrade quietly: no URI to put on the
    # event, and a callback that is safe to invoke rather than one that raises.
    uri, upload_description = ErrorAttachmentsS3Uploader().prepare_description_upload("boom")
    assert uri is None
    upload_description()


def test_submit_manual_bug_report_uploads_nothing_for_an_empty_description() -> None:
    # A zero-byte object referenced from the event would be pure noise on every such report, and the
    # caller already rejects empty descriptions -- so the event still goes, with no URI to follow.
    uploader = _RecordingUploader()
    with registered_attachments_uploader(uploader), capturing_sentry_client() as captured_events:
        submit_manual_bug_report(title="[bug report] boom", description="", report={}, logs_folder=None)

    assert uploader.prepared_descriptions == []
    assert len(captured_events) == 1
    assert BUG_REPORT_DESCRIPTION_EXTRA_KEY not in cast(dict, captured_events[0]["extra"])


@pytest.fixture
def _cleanup_ignored_loggers() -> Iterator[list[str]]:
    # ``ignore_logger`` mutates a process-global sentry registry, so any patterns a test registers
    # must be reverted afterward to avoid leaking into other tests.
    registered: list[str] = []
    yield registered
    for pattern in registered:
        unignore_logger(pattern)


def test_register_ignored_loggers_makes_matching_loggers_ignored(_cleanup_ignored_loggers: list[str]) -> None:
    # The default LoggingIntegration captures ERROR-level stdlib records as Sentry events even for
    # loggers with propagate=False (it patches Logger.callHandlers at the class level). Registering a
    # glob pattern must make its EventHandler drop matching records -- both the exact name and any
    # child logger -- while leaving unrelated loggers alone.
    patterns = ["paramiko", "paramiko.*"]
    _cleanup_ignored_loggers.extend(patterns)
    _register_ignored_loggers(patterns)

    handler = EventHandler(level=logging.ERROR)
    assert handler._can_record(logging.makeLogRecord({"name": "paramiko"})) is False
    assert handler._can_record(logging.makeLogRecord({"name": "paramiko.transport"})) is False
    assert handler._can_record(logging.makeLogRecord({"name": "imbue.minds"})) is True


def test_submit_manual_bug_report_returns_none_when_sentry_inactive() -> None:
    # With no active Sentry client (the default in tests), the submit is a no-op that returns None
    # (no event id) rather than raising.
    assert submit_manual_bug_report(title="t", description="d", report={"description": "d"}, logs_folder=None) is None
