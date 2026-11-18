from pathlib import Path

import pytest

from imbue.minds.desktop_client.update_channel import ReleaseChannel
from imbue.minds.desktop_client.update_channel import UPDATE_CHANNEL_FILENAME
from imbue.minds.desktop_client.update_channel import read_release_channel


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('{\n  "channel": "alpha"\n}\n', ReleaseChannel.ALPHA),
        ('{"channel": "beta"}', ReleaseChannel.BETA),
        ('{"channel": "stable"}', ReleaseChannel.STABLE),
    ],
)
def test_reads_the_channel_the_shell_recorded(tmp_path: Path, stored: str, expected: ReleaseChannel) -> None:
    (tmp_path / UPDATE_CHANNEL_FILENAME).write_text(stored)

    assert read_release_channel(tmp_path, is_update_feed_configured=True) is expected


@pytest.mark.parametrize(
    ("stored", "is_update_feed_configured"),
    [
        (None, True),
        ("", True),
        ("not json", True),
        ('["alpha"]', True),
        ("{}", True),
        ('{"channel": null}', True),
        ('{"channel": "ALPHA"}', True),
        ('{"channel": "nightly"}', True),
        ('{"channel": "alpha"}', False),
        ('{"channel": "beta"}', False),
    ],
    ids=[
        "missing",
        "empty",
        "malformed",
        "not-an-object",
        "no-channel",
        "null",
        "wrong-case",
        "unknown",
        "alpha-without-a-feed",
        "beta-without-a-feed",
    ],
)
def test_reads_stable_whenever_the_shell_would(
    tmp_path: Path, stored: str | None, is_update_feed_configured: bool
) -> None:
    """The shell falls back to stable for each of these, so the backend must read the same channel it does."""
    if stored is not None:
        (tmp_path / UPDATE_CHANNEL_FILENAME).write_text(stored)

    assert read_release_channel(tmp_path, is_update_feed_configured=is_update_feed_configured) is ReleaseChannel.STABLE
