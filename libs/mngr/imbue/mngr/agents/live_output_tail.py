from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Callable

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.live_output import LIVE_OUTPUT_POLL_INTERVAL
from imbue.mngr.interfaces.live_output import LIVE_OUTPUT_POLL_TIMEOUT
from imbue.mngr.interfaces.live_output import LiveOutputReader
from imbue.mngr.utils.polling import poll_until


def _read_text_or_none(host: OnlineHostInterface, path: Path) -> str | None:
    """Read ``path`` via ``host``, returning None if it does not exist yet."""
    try:
        return host.read_text_file(path)
    except FileNotFoundError:
        return None


class _LiveOutputTailer(MutableModel):
    """Polls a live-output file and yields the text deltas a reader extracts from it.

    The new-data check is a bound method reading ``self.last_mtime`` (mutated on
    the model, not a captured local), so it can be handed to ``poll_until``
    directly without wrapping per-call state in a closure.
    """

    host: OnlineHostInterface
    path: Path
    is_finished: Callable[[], bool]
    last_mtime: datetime | None = None

    def has_new_data_or_finished(self) -> bool:
        current_mtime = self.host.get_file_mtime(self.path)
        if current_mtime is not None and current_mtime != self.last_mtime:
            return True
        return self.is_finished()

    def tail(self, reader: LiveOutputReader) -> Iterator[str]:
        while not reader.is_complete and not self.is_finished():
            poll_until(
                self.has_new_data_or_finished,
                timeout=LIVE_OUTPUT_POLL_TIMEOUT,
                poll_interval=LIVE_OUTPUT_POLL_INTERVAL,
            )
            self.last_mtime = self.host.get_file_mtime(self.path)
            content = _read_text_or_none(self.host, self.path)
            if content is not None:
                yield from reader.feed(content)

        # Final drain after the agent exits, unless a terminal marker already
        # ended the stream (in which case trailing bytes are not ours to emit).
        if not reader.is_complete:
            content = _read_text_or_none(self.host, self.path)
            if content is not None:
                yield from reader.feed(content)
            if not reader.is_complete:
                yield from reader.finalize()


def tail_live_output(
    host: OnlineHostInterface,
    path: Path,
    reader: LiveOutputReader,
    is_finished: Callable[[], bool],
) -> Iterator[str]:
    """Tail ``path`` on ``host``, yielding text deltas until ``is_finished()``.

    The caller supplies the ``reader`` (rather than this building it) so it can
    inspect the reader's terminal state -- :attr:`LiveOutputReader.is_complete` /
    :attr:`LiveOutputReader.stream_error` -- once streaming finishes.
    """
    tailer = _LiveOutputTailer(host=host, path=path, is_finished=is_finished)
    yield from tailer.tail(reader)
