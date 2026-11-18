"""The release channel this install takes app updates from, and what it means for workspace updates.

The Electron shell owns the choice (``electron/update-channel.js``) and records it in the state root
it shares with this backend. The desktop client is a subprocess the shell spawns, with no channel
from the shell back to it, so the file is read directly, on every detection sweep: a switch applies
without a restart.
"""

import json
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum


class ReleaseChannel(UpperCaseStrEnum):
    """Which release channel the app takes its own updates from."""

    STABLE = auto()
    BETA = auto()
    ALPHA = auto()


# Written by electron/update-channel.js; kept in step with ``PREFERENCE_FILENAME`` there.
UPDATE_CHANNEL_FILENAME: Final[str] = "update-channel.json"

# The shell stores each channel by its lowercase name (``CHANNELS`` in electron/update-channel.js).
_CHANNEL_BY_STORED_NAME: Final[dict[str, ReleaseChannel]] = {
    channel.value.lower(): channel for channel in ReleaseChannel
}


def read_release_channel(state_dir: Path, *, is_update_feed_configured: bool) -> ReleaseChannel:
    """The channel the shell recorded, or stable when it recorded none, one this build does not know, or one
    this build cannot serve.

    These are the shell's own fallbacks (``readChannel``), so the two read the same channel. A build whose
    client config names no ``update_feed_base_url`` serves stable alone (``availableChannels``).
    """
    channel_path = state_dir / UPDATE_CHANNEL_FILENAME
    try:
        raw = json.loads(channel_path.read_text())
    except FileNotFoundError:
        return ReleaseChannel.STABLE
    except (OSError, ValueError) as e:
        logger.warning("Reading the update channel as stable: {} is unreadable: {}", channel_path, e)
        return ReleaseChannel.STABLE
    stored_name = raw.get("channel") if isinstance(raw, dict) else None
    channel = _CHANNEL_BY_STORED_NAME.get(stored_name) if isinstance(stored_name, str) else None
    if channel is None:
        logger.warning("Reading the update channel as stable: {} names no known channel", channel_path)
        return ReleaseChannel.STABLE
    if channel is not ReleaseChannel.STABLE and not is_update_feed_configured:
        logger.warning(
            "Reading the update channel as stable: {} names {}, which this build does not serve", channel_path, channel
        )
        return ReleaseChannel.STABLE
    return channel


def is_every_release_offered(channel: ReleaseChannel) -> bool:
    """Whether a workspace behind the app by only a patch or prerelease is offered its update on this channel."""
    match channel:
        case ReleaseChannel.ALPHA:
            return True
        case ReleaseChannel.STABLE | ReleaseChannel.BETA:
            return False
        case _ as unreachable:
            assert_never(unreachable)
