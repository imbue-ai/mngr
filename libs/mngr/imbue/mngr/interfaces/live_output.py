from __future__ import annotations

from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel

# Poll cadence for tailing a live-output file: check for new bytes every
# interval, giving up only after timeout (long, since a turn can take minutes).
LIVE_OUTPUT_POLL_INTERVAL: float = 0.05
LIVE_OUTPUT_POLL_TIMEOUT: float = 300.0


class LiveOutputReader(MutableModel, ABC):
    """Extracts incremental assistant text from successive full reads of a live-output file.

    Each agent kind supplies a reader matched to its on-disk format (e.g.
    :class:`RawTextReader` for an append-only log, a stream-json reader for
    ``claude --print``, or a snapshot-diff reader for a TUI watcher's buffer).
    The caller reads the *full* file contents and passes them to :meth:`feed`,
    which returns the newly-available text chunks since the previous call,
    withholding any still-volatile tail; :meth:`finalize` releases that tail
    once at stream/turn end. Both return a list so a single read that uncovers
    several units of text (e.g. multiple stream-json events) yields them
    separately rather than glued together; the list is empty when nothing new
    is available.

    Readers hold only extraction state -- file IO and polling belong to the
    caller -- so the same reader serves both pull consumers (the shared tail
    loop in :func:`imbue.mngr.agents.live_output_tail.tail_live_output`) and push
    consumers (drivers that poll the reader on their own multi-turn cadence).
    """

    @abstractmethod
    def feed(self, content: str) -> list[str]:
        """Return newly-available text chunks given the full current file contents.

        Withholds any still-volatile tail (a partial trailing line, a churning
        final rendered line) so it is not emitted mid-stream; that tail is
        released by :meth:`finalize`. Returns an empty list when nothing new is
        available.
        """
        ...

    def finalize(self) -> list[str]:
        """Release any withheld tail text at stream/turn end (default: none)."""
        return []

    @property
    def is_complete(self) -> bool:
        """Whether the content has signalled end-of-stream.

        When True the tail loop stops polling and skips the final drain. Only
        formats with an explicit terminal marker (e.g. claude stream-json's
        ``result`` event) override this; the default never completes on its own
        and relies on the caller's ``is_finished`` predicate.
        """
        return False

    @property
    def stream_error(self) -> str | None:
        """Error text surfaced by the stream's terminal marker, if any (default: none)."""
        return None


class RawTextReader(LiveOutputReader):
    """Reader for an append-only raw-text file: new text is whatever was appended.

    Tracks how many characters have been consumed and returns the suffix on each
    feed. Nothing is withheld (raw text has no volatile tail), so there is no
    terminal marker, no error signal, and nothing to release in finalize.
    """

    chars_consumed: int = 0

    def feed(self, content: str) -> list[str]:
        new_text = content[self.chars_consumed :]
        self.chars_consumed = len(content)
        return [new_text] if new_text else []
