"""Tests for the byte-offset transcript tailer (rotation + partial lines)."""

from __future__ import annotations

from imbue.mngr_foreman.transcript_tail import TranscriptTailer


class _FakeFile:
    """A stand-in for a remote read_file: returns whatever bytes it currently holds."""

    def __init__(self) -> None:
        self.content = b""

    def append(self, text: str) -> None:
        self.content += text.encode("utf-8")

    def rotate(self, text: str = "") -> None:
        """Simulate log rotation: the current file shrinks (starts fresh)."""
        self.content = text.encode("utf-8")

    def reader(self) -> bytes:
        return self.content


def test_backfill_then_incremental() -> None:
    f = _FakeFile()
    f.append("a\nb\n")
    t = TranscriptTailer(f.reader)
    assert t.poll() == ["a", "b"]
    # No change -> nothing new.
    assert t.poll() == []
    f.append("c\n")
    assert t.poll() == ["c"]


def test_partial_line_held_until_newline() -> None:
    f = _FakeFile()
    f.append("full\npart")  # "part" has no newline yet
    t = TranscriptTailer(f.reader)
    assert t.poll() == ["full"]
    assert t.byte_offset == len(b"full\n")
    # The partial line is completed on a later flush.
    f.append("ial\n")
    assert t.poll() == ["partial"]


def test_rotation_resets_offset_and_rereads() -> None:
    f = _FakeFile()
    f.append("one\ntwo\n")
    t = TranscriptTailer(f.reader)
    assert t.poll() == ["one", "two"]
    offset_before = t.byte_offset
    assert offset_before > 0
    # Rotation: file shrinks to a fresh, shorter content.
    f.rotate("three\n")
    lines = t.poll()
    assert lines == ["three"]
    # Offset was reset to 0 then advanced over the new file only.
    assert t.byte_offset == len(b"three\n")


def test_rotation_to_empty_then_grows() -> None:
    f = _FakeFile()
    f.append("x\n")
    t = TranscriptTailer(f.reader)
    assert t.poll() == ["x"]
    f.rotate("")  # rotated to empty
    assert t.poll() == []
    assert t.byte_offset == 0
    f.append("y\n")
    assert t.poll() == ["y"]


def test_read_error_returns_empty_without_advancing() -> None:
    fail = {"value": False}
    content = {"value": b"line\n"}

    def reader() -> bytes:
        if fail["value"]:
            raise OSError("host briefly unreachable")
        return content["value"]

    t = TranscriptTailer(reader)
    assert t.poll() == ["line"]
    offset = t.byte_offset
    fail["value"] = True
    assert t.poll() == []  # error tolerated
    assert t.byte_offset == offset
    fail["value"] = False
    content["value"] = b"line\nline2\n"
    assert t.poll() == ["line2"]


def test_multibyte_partial_line() -> None:
    f = _FakeFile()
    f.append("héllo\n")  # multi-byte char, complete line
    t = TranscriptTailer(f.reader)
    assert t.poll() == ["héllo"]
    assert t.byte_offset == len("héllo\n".encode("utf-8"))
