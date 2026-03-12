"""Unit tests for conversation_db.py."""

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from imbue.mng_claude_mind.conftest import create_mind_conversations_table_in_test_db
from imbue.mng_claude_mind.resources.conversation_db import _warn
from imbue.mng_claude_mind.resources.conversation_db import _write_stdout
from imbue.mng_claude_mind.resources.conversation_db import count
from imbue.mng_claude_mind.resources.conversation_db import insert
from imbue.mng_claude_mind.resources.conversation_db import lookup_model
from imbue.mng_claude_mind.resources.conversation_db import main
from imbue.mng_claude_mind.resources.conversation_db import max_rowid
from imbue.mng_claude_mind.resources.conversation_db import poll_new


def _create_db(db_path: Path) -> None:
    """Create a test database with both required tables using shared infrastructure."""
    create_mind_conversations_table_in_test_db(db_path)


def test_write_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _write_stdout("hello")
    captured = capsys.readouterr()
    assert captured.out == "hello\n"


def test_write_stdout_with_int(capsys: pytest.CaptureFixture[str]) -> None:
    _write_stdout(42)
    captured = capsys.readouterr()
    assert captured.out == "42\n"


def test_warn(capsys: pytest.CaptureFixture[str]) -> None:
    _warn("something broke")
    captured = capsys.readouterr()
    assert captured.err == "WARNING: something broke\n"
    assert captured.out == ""


def test_insert_creates_record(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    insert(str(db_path), "conv-1", '{"env":"prod"}', "2025-01-15T10:00:00Z")

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT conversation_id, tags, created_at FROM mind_conversations WHERE conversation_id = ?",
            ("conv-1",),
        ).fetchone()
    assert row is not None
    assert row[0] == "conv-1"
    assert row[1] == '{"env":"prod"}'
    assert row[2] == "2025-01-15T10:00:00Z"


def test_insert_replaces_existing(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    insert(str(db_path), "conv-1", "{}", "2025-01-01T00:00:00Z")
    insert(str(db_path), "conv-1", '{"updated":true}', "2025-01-02T00:00:00Z")

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT tags, created_at FROM mind_conversations WHERE conversation_id = ?",
            ("conv-1",),
        ).fetchone()
    assert row is not None
    assert row[0] == '{"updated":true}'
    assert row[1] == "2025-01-02T00:00:00Z"


def test_insert_creates_table_if_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    insert(str(db_path), "conv-new", "{}", "2025-06-01T00:00:00Z")

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
            ("conv-new",),
        ).fetchone()
    assert row is not None


def test_lookup_model_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO conversations (id, name, model) VALUES (?, ?, ?)",
            ("conv-1", "test", "claude-sonnet-4-6"),
        )
        conn.commit()

    lookup_model(str(db_path), "conv-1")
    captured = capsys.readouterr()
    assert captured.out == "claude-sonnet-4-6\n"


def test_lookup_model_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)

    lookup_model(str(db_path), "nonexistent")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_lookup_model_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "missing.db"
    lookup_model(str(db_path), "conv-1")
    captured = capsys.readouterr()
    assert "WARNING:" in captured.err
    assert captured.out == ""


def test_count_with_records(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    insert(str(db_path), "conv-1", "{}", "2025-01-01T00:00:00Z")
    insert(str(db_path), "conv-2", "{}", "2025-01-02T00:00:00Z")

    count(str(db_path))
    captured = capsys.readouterr()
    assert captured.out == "2\n"


def test_count_empty_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)

    count(str(db_path))
    captured = capsys.readouterr()
    assert captured.out == "0\n"


def test_count_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "missing.db"
    count(str(db_path))
    captured = capsys.readouterr()
    assert "WARNING:" in captured.err
    assert captured.out == "0\n"


def test_max_rowid_with_records(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("INSERT INTO conversations (id, name, model) VALUES ('a', 'a', 'm1')")
        conn.execute("INSERT INTO conversations (id, name, model) VALUES ('b', 'b', 'm2')")
        conn.commit()

    max_rowid(str(db_path))
    captured = capsys.readouterr()
    assert captured.out == "2\n"


def test_max_rowid_empty_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)

    max_rowid(str(db_path))
    captured = capsys.readouterr()
    assert captured.out == "0\n"


def test_max_rowid_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "missing.db"
    max_rowid(str(db_path))
    captured = capsys.readouterr()
    assert "WARNING:" in captured.err
    assert captured.out == "0\n"


def test_poll_new_finds_new_conversation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("INSERT INTO conversations (id, name, model) VALUES ('old', 'old', 'm1')")
        conn.execute("INSERT INTO conversations (id, name, model) VALUES ('new', 'new', 'm2')")
        conn.commit()

    poll_new(str(db_path), "1")
    captured = capsys.readouterr()
    assert captured.out == "new\n"


def test_poll_new_no_new_conversations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("INSERT INTO conversations (id, name, model) VALUES ('only', 'only', 'm1')")
        conn.commit()

    poll_new(str(db_path), "1")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_poll_new_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "missing.db"
    poll_new(str(db_path), "0")
    captured = capsys.readouterr()
    assert "WARNING:" in captured.err


@contextmanager
def _override_argv(new_argv: list[str]) -> Iterator[None]:
    """Temporarily replace sys.argv, restoring on exit."""
    original = sys.argv
    sys.argv = new_argv
    try:
        yield
    finally:
        sys.argv = original


def test_main_insert(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    with _override_argv(["conversation_db", "insert", str(db_path), "conv-main", "{}", "2025-03-01T00:00:00Z"]):
        main()

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
            ("conv-main",),
        ).fetchone()
    assert row is not None


def test_main_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    insert(str(db_path), "c1", "{}", "2025-01-01T00:00:00Z")

    with _override_argv(["conversation_db", "count", str(db_path)]):
        main()

    captured = capsys.readouterr()
    assert captured.out == "1\n"


def test_main_unknown_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with _override_argv(["conversation_db", "bogus", "/tmp/x.db"]):
        with pytest.raises(SystemExit, match="1"):
            main()

    captured = capsys.readouterr()
    assert "Unknown subcommand: bogus" in captured.err


def test_main_too_few_args(capsys: pytest.CaptureFixture[str]) -> None:
    with _override_argv(["conversation_db", "count"]):
        with pytest.raises(SystemExit, match="1"):
            main()

    captured = capsys.readouterr()
    assert "Usage:" in captured.err
