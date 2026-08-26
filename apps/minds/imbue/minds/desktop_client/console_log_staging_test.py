"""Unit coverage for reading the Electron shell's captured console.

The capture writes an append-only, size-rotating file (``electron/console-capture.js``), so the reader
has to take the newest slice rather than the whole thing: the file is bounded at 10MB, but a report
wants the console around the bug, not the app's whole history. These tests pin the two properties
that follow from that -- a bounded read from the END of the file, and no half-record at the seam --
plus the empty-file case the rotating stream introduced, since it creates the file at startup rather
than on the first message.
"""

from pathlib import Path

from imbue.minds.desktop_client.console_log_staging import ELECTRON_CONSOLE_TAIL_FILENAME
from imbue.minds.desktop_client.console_log_staging import MAX_CONSOLE_TAIL_BYTES
from imbue.minds.desktop_client.console_log_staging import read_console_tail


def _write_console(logs_dir: Path, contents: str) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / ELECTRON_CONSOLE_TAIL_FILENAME
    path.write_text(contents, encoding="utf-8")
    return path


def test_a_console_that_was_never_captured_reads_as_nothing(tmp_path: Path) -> None:
    assert read_console_tail(tmp_path) is None


def test_an_empty_console_file_reads_as_nothing_rather_than_an_empty_attachment(tmp_path: Path) -> None:
    """The capture opens its stream at startup, so the file exists before any message is logged.

    An empty file must report as no console -- returning "" would instead ship an empty attachment,
    because the collector treats only None as "the shell captured nothing".
    """
    _write_console(tmp_path, "")

    assert read_console_tail(tmp_path) is None


def test_a_short_console_is_read_whole(tmp_path: Path) -> None:
    _write_console(tmp_path, "first message\nsecond message\n")

    tail = read_console_tail(tmp_path)

    assert tail is not None
    assert tail.splitlines() == ["first message", "second message"]


def test_only_the_end_of_a_large_console_is_read(tmp_path: Path) -> None:
    """A rotating file reaches 10MB, and a report wants the console around the bug, not all of it."""
    filler = "\n".join(f"old-{index}".ljust(120, ".") for index in range(MAX_CONSOLE_TAIL_BYTES // 100))
    _write_console(tmp_path, filler + "\nnewest message\n")

    tail = read_console_tail(tmp_path)

    assert tail is not None
    assert len(tail.encode("utf-8")) <= MAX_CONSOLE_TAIL_BYTES
    assert "newest message" in tail
    assert "old-0." not in tail, "the oldest records must have been left behind"


def test_a_record_torn_by_the_seek_is_dropped_rather_than_reported_as_a_message(tmp_path: Path) -> None:
    """Seeking to a byte offset lands mid-record, so the first line back is a fragment."""
    tail_marker = "-TAIL-END-OF-A-VERY-LONG-RECORD"
    _write_console(tmp_path, "x" * (MAX_CONSOLE_TAIL_BYTES + 5_000) + tail_marker + "\nintact message\n")

    tail = read_console_tail(tmp_path)

    assert tail is not None
    assert tail_marker not in tail, "the fragment of a torn record was reported as if it were a message"
    assert tail.splitlines() == ["intact message"]


def test_the_excerpt_is_bounded_by_bytes_alone_keeping_the_newest_records(tmp_path: Path) -> None:
    """The one bound is the byte cap -- a file-size-style limit, with no count of
    lines or per-message characters layered on top."""
    record = "msg-{index:07d}" + "." * 100
    total = (MAX_CONSOLE_TAIL_BYTES // len(record)) + 500
    _write_console(tmp_path, "".join(record.format(index=index) + "\n" for index in range(total)))

    tail = read_console_tail(tmp_path)

    assert tail is not None
    assert len(tail.encode("utf-8")) <= MAX_CONSOLE_TAIL_BYTES
    lines = tail.splitlines()
    assert lines[-1].startswith(f"msg-{total - 1:07d}")
    assert len(lines) > 1


def test_an_unreadable_console_costs_the_attachment_rather_than_the_report(tmp_path: Path) -> None:
    """A directory where the file should be: the read fails as an OSError that is not FileNotFound."""
    (tmp_path / ELECTRON_CONSOLE_TAIL_FILENAME).mkdir(parents=True)

    assert read_console_tail(tmp_path) is None
