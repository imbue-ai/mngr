"""Test helpers for the shared Sentry machinery.

Per CLAUDE.md, this module has no test of its own; it is exercised through the tests that import it.
"""

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import PrivateAttr
from sentry_sdk import Client
from sentry_sdk import isolation_scope
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event
from sentry_sdk.types import Hint

import imbue.imbue_common.sentry.core as sentry_core
import imbue.imbue_common.sentry.s3_uploader as s3_uploader

# A test DSN: well-formed enough for the SDK to consider the client active, pointing nowhere.
_TEST_DSN = "https://public@example.com/1"

# The bucket ``recording_s3_bucket`` pretends is configured. Exposed so a test can spell out the
# ``s3://`` URI it expects rather than deriving it the same way the code under test does.
TEST_S3_BUCKET = "test-bug-reports-bucket"


class _CapturingTransport(Transport):
    """A Sentry transport that records every delivered event instead of sending it anywhere."""

    def __init__(self, captured_events: list[Event]) -> None:
        super().__init__()
        self._captured_events = captured_events

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self._captured_events.append(event)


@contextmanager
def capturing_sentry_client(
    before_send: Callable[[Event, Hint], Event | None] | None = None,
) -> Iterator[list[Event]]:
    """Bind an isolated, active Sentry client that appends delivered events to the yielded list.

    Capture is synchronous, so the yielded list is already populated inside the block.
    """
    # Mirror setup_sentry's serializer configuration so captured events are trimmed (or, for real
    # event shapes, not trimmed) exactly as production events are.
    sentry_core.raise_sentry_databag_breadth()
    captured_events: list[Event] = []
    client = Client(
        dsn=_TEST_DSN,
        before_send=before_send,
        transport=_CapturingTransport(captured_events),
        default_integrations=False,
        auto_enabling_integrations=False,
        send_default_pii=False,
    )
    with isolation_scope() as scope:
        scope.set_client(client)
        try:
            yield captured_events
        finally:
            # Nothing is queued behind this (the capturing transport records inline), so this only
            # keeps the helper honest about leaving no client work outstanding however the block exits.
            client.flush()


@contextmanager
def registered_attachments_uploader(uploader: sentry_core.ErrorAttachmentsS3Uploader) -> Iterator[None]:
    """Make ``uploader`` the process-wide attachments uploader for the duration of the block.

    ``submit_manual_bug_report`` consults that process-global before it does anything else, so a test
    that leaves one behind changes how later tests in the same worker behave.
    """
    previous = sentry_core.get_attachments_uploader()
    sentry_core.register_attachments_uploader(uploader)
    try:
        yield
    finally:
        sentry_core.register_attachments_uploader(previous)


class _RecordingS3Uploader(s3_uploader._S3Uploader):
    """An uploader that records what each upload would have written instead of contacting S3.

    Overriding at ``upload_if_possible`` (rather than lower down) means nothing reaches the thread
    pool or the network, while the key -> ``s3://`` URI mapping stays the real one.
    """

    _uploads: list[tuple[str, bytes]] = PrivateAttr(default_factory=list)

    def upload_if_possible(self, key: str, contents: bytes) -> str | None:
        self._uploads.append((key, contents))
        return self.s3_uri_from_key(key)


@contextmanager
def recording_s3_bucket() -> Iterator[list[tuple[str, bytes]]]:
    """Make S3 uploads resolve against ``TEST_S3_BUCKET``, yielding the ``(key, contents)`` recorded.

    Without this, every ``s3://`` URI in the suite is None and every upload a no-op, so a test cannot
    tell an upload that went to the key its URI names from one that went somewhere else. The uploader
    is a process-global that ``setup_s3_uploads`` only ever sets once, so it is swapped and restored
    here rather than configured, to keep it out of later tests in the same worker.
    """
    uploader = _RecordingS3Uploader(bucket=TEST_S3_BUCKET, region=s3_uploader.DEFAULT_REGION)
    previous = s3_uploader._S3_UPLOADER
    s3_uploader._S3_UPLOADER = uploader
    try:
        yield uploader._uploads
    finally:
        s3_uploader._S3_UPLOADER = previous
