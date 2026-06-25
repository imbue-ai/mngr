"""Tests for the coordinator script."""

from pathlib import Path

import pytest

from scripts.josh.coordinator import ProcessManager
from scripts.josh.coordinator import Task
from scripts.josh.coordinator import normalize_task_name
from scripts.josh.coordinator import parse_sections
from scripts.josh.coordinator import parse_task_file
from scripts.josh.coordinator import parse_tasks
from scripts.josh.coordinator import process_tasks
from scripts.josh.coordinator import read_json_file
from scripts.josh.coordinator import write_json_file

# --- normalize_task_name ---


def test_normalize_task_name_passes_through_already_normalized_name() -> None:
    assert normalize_task_name("first-task") == "first-task"


def test_normalize_task_name_converts_spaces_to_hyphens() -> None:
    assert normalize_task_name("even easier task") == "even-easier-task"


def test_normalize_task_name_converts_long_name_with_spaces() -> None:
    assert (
        normalize_task_name("another task to demonstrate that spaces are ok in task names")
        == "another-task-to-demonstrate-that-spaces-are-ok-in-task-names"
    )


def test_normalize_task_name_lowercases_mixed_case() -> None:
    assert normalize_task_name("a final task I forgot") == "a-final-task-i-forgot"
    assert normalize_task_name("Task #1!") == "task-1"


def test_normalize_task_name_converts_special_characters_to_hyphens() -> None:
    assert normalize_task_name("task#1!") == "task-1"
    assert normalize_task_name("my@task$name") == "my-task-name"


def test_normalize_task_name_collapses_consecutive_hyphens() -> None:
    assert normalize_task_name("task---with---hyphens") == "task-with-hyphens"
    assert normalize_task_name("task   with   spaces") == "task-with-spaces"


def test_normalize_task_name_strips_leading_and_trailing_hyphens() -> None:
    assert normalize_task_name("-task-") == "task"
    assert normalize_task_name("---task---") == "task"


def test_normalize_task_name_raises_when_result_is_empty() -> None:
    with pytest.raises(ValueError, match="empty string"):
        normalize_task_name("!!!")
    with pytest.raises(ValueError, match="empty string"):
        normalize_task_name("---")
    with pytest.raises(ValueError, match="empty string"):
        normalize_task_name("   ")


# --- parse_sections ---


def test_parse_sections_parses_single_section() -> None:
    content = """goal:
demonstrate this format

"""
    assert parse_sections(content) == {"goal": "demonstrate this format"}


def test_parse_sections_parses_multiple_sections() -> None:
    content = """goal:
first section

reminder:
second section
"""
    assert parse_sections(content) == {
        "goal": "first section",
        "reminder": "second section",
    }


def test_parse_sections_concatenates_repeated_section_headers() -> None:
    content = """foo:
first content

bar:
middle section

foo:
second content
"""
    assert parse_sections(content) == {
        "foo": "first content\nsecond content",
        "bar": "middle section",
    }


def test_parse_sections_preserves_multi_line_section_content() -> None:
    content = """reminder:
pay attention to all of the instructions!
    there might be details
and it's important to get everything right

"""
    expected = "pay attention to all of the instructions!\n    there might be details\nand it's important to get everything right"
    assert parse_sections(content)["reminder"] == expected


def test_parse_sections_returns_empty_for_empty_content() -> None:
    assert parse_sections("") == {}


def test_parse_sections_returns_empty_when_no_section_headers() -> None:
    content = """just some text
without any sections
"""
    assert parse_sections(content) == {}


def test_parse_sections_handles_section_ending_at_eof_without_blank_line() -> None:
    content = """task:
some task"""
    assert parse_sections(content) == {"task": "some task"}


# --- parse_tasks ---


def test_parse_tasks_parses_single_task_with_content() -> None:
    tasks = parse_tasks("first-task\n    do something easy")
    assert len(tasks) == 1
    assert tasks[0].name == "first-task"
    assert tasks[0].content == "do something easy"


def test_parse_tasks_parses_task_without_content() -> None:
    tasks = parse_tasks("even-easier-task-with-no-description")
    assert len(tasks) == 1
    assert tasks[0].name == "even-easier-task-with-no-description"
    assert tasks[0].content == ""


def test_parse_tasks_parses_multiple_tasks() -> None:
    task_content = """first-task
    do something easy
second-task
    do something else"""
    tasks = parse_tasks(task_content)
    assert len(tasks) == 2
    assert tasks[0].name == "first-task"
    assert tasks[0].content == "do something easy"
    assert tasks[1].name == "second-task"
    assert tasks[1].content == "do something else"


def test_parse_tasks_strips_only_first_four_spaces_of_indentation() -> None:
    task_content = """another task to demonstrate that spaces are ok in task names
    and obviously
        there can be
            lots of indentation
    but remember to remove only the first 4 spaces"""
    tasks = parse_tasks(task_content)
    assert len(tasks) == 1
    assert tasks[0].name == "another-task-to-demonstrate-that-spaces-are-ok-in-task-names"
    expected_content = (
        "and obviously\n    there can be\n        lots of indentation\nbut remember to remove only the first 4 spaces"
    )
    assert tasks[0].content == expected_content


def test_parse_tasks_returns_empty_for_empty_section() -> None:
    assert parse_tasks("") == []


def test_parse_tasks_parses_complete_example_from_spec() -> None:
    task_content = """first-task
    do something easy
even-easier-task-with-no-description
another task to demonstrate that spaces are ok in task names
    and obviously
        there can be
            lots of indentation
    but remember to remove only the first 4 spaces
a-final-task-I-forgot
    with some details
    and text"""
    tasks = parse_tasks(task_content)
    assert len(tasks) == 4

    assert tasks[0].name == "first-task"
    assert tasks[0].content == "do something easy"

    assert tasks[1].name == "even-easier-task-with-no-description"
    assert tasks[1].content == ""

    assert tasks[2].name == "another-task-to-demonstrate-that-spaces-are-ok-in-task-names"
    expected = (
        "and obviously\n    there can be\n        lots of indentation\nbut remember to remove only the first 4 spaces"
    )
    assert tasks[2].content == expected

    assert tasks[3].name == "a-final-task-i-forgot"
    assert tasks[3].content == "with some details\nand text"


def test_parse_tasks_skips_tasks_with_invalid_names() -> None:
    task_content = """valid-task
    content here
!!!
    this should be skipped
another-valid-task
    more content"""
    tasks = parse_tasks(task_content)
    assert len(tasks) == 2
    assert tasks[0].name == "valid-task"
    assert tasks[1].name == "another-valid-task"


# --- parse_task_file ---


def test_parse_task_file_parses_complete_example_from_spec(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """goal:
demonstrate this format

reminder:
pay attention to all of the instructions!
    there might be details
and it's important to get everything right

task:
first-task
    do something easy
even-easier-task-with-no-description
another task to demonstrate that spaces are ok in task names
    and obviously
        there can be
            lots of indentation
    but remember to remove only the first 4 spaces

reminder:
probably a good idea to write a little test too

task:
a-final-task-I-forgot
    with some details
    and text
""",
        encoding="utf-8",
    )

    tasks = parse_task_file(task_file)
    assert len(tasks) == 4
    assert tasks[0].name == "first-task"
    assert tasks[1].name == "even-easier-task-with-no-description"
    assert tasks[2].name == "another-task-to-demonstrate-that-spaces-are-ok-in-task-names"
    assert tasks[3].name == "a-final-task-i-forgot"


# --- Task ---


def test_task_to_dict_sorts_keys() -> None:
    task = Task(name="test-task", content="test content")
    task_dict = task.to_dict()
    assert task_dict == {"content": "test content", "name": "test-task"}
    assert list(task_dict.keys()) == ["content", "name"]


def test_task_equality_compares_name_and_content() -> None:
    task1 = Task(name="task", content="content")
    task2 = Task(name="task", content="content")
    task3 = Task(name="task", content="different")
    task4 = Task(name="different", content="content")

    assert task1 == task2
    assert task1 != task3
    assert task1 != task4
    assert task1 != "not a task"


# --- JSON file operations ---


def test_write_json_file_then_read_json_file_round_trips(tmp_path: Path) -> None:
    json_file = tmp_path / "test.json"
    data = {"content": "test content", "name": "test-task"}

    write_json_file(json_file, data)
    assert json_file.exists()

    assert read_json_file(json_file) == data


def test_write_json_file_uses_two_space_indent_and_sorted_keys(tmp_path: Path) -> None:
    json_file = tmp_path / "test.json"
    write_json_file(json_file, {"content": "test", "name": "task"})

    content = json_file.read_text(encoding="utf-8")
    expected = '{\n  "content": "test",\n  "name": "task"\n}\n'
    assert content == expected


def test_read_json_file_returns_none_for_nonexistent_file(tmp_path: Path) -> None:
    assert read_json_file(tmp_path / "nonexistent.json") is None


def test_read_json_file_returns_none_for_malformed_json(tmp_path: Path) -> None:
    json_file = tmp_path / "malformed.json"
    json_file.write_text("not valid json", encoding="utf-8")
    assert read_json_file(json_file) is None


# --- process_tasks / ProcessManager integration ---


def test_process_tasks_initial_sync_creates_json_files_with_content(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
task-one
    content for task one
task-two
    content for task two
""",
        encoding="utf-8",
    )
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    process_manager = ProcessManager("echo {json_file}")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)

    assert task_names == {"task-one", "task-two"}
    assert read_json_file(task_dir / "task-one.json") == {"content": "content for task one", "name": "task-one"}
    assert read_json_file(task_dir / "task-two.json") == {"content": "content for task two", "name": "task-two"}


def test_process_tasks_rewrites_json_when_task_content_changes(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    process_manager = ProcessManager("echo {json_file}")

    task_file.write_text("task:\nmy-task\n    original content\n", encoding="utf-8")
    process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
    json_path = task_dir / "my-task.json"
    assert read_json_file(json_path) == {"content": "original content", "name": "my-task"}

    # Re-running with the same content must NOT rewrite the file (no-change branch).
    mtime_before = json_path.stat().st_mtime_ns
    process_tasks(task_file, task_dir, process_manager, previous_task_names={"my-task"})
    assert json_path.stat().st_mtime_ns == mtime_before

    # Changing the task body must rewrite the JSON with the new content.
    task_file.write_text("task:\nmy-task\n    updated content\n", encoding="utf-8")
    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names={"my-task"})
    assert task_names == {"my-task"}
    assert read_json_file(json_path) == {"content": "updated content", "name": "my-task"}


def test_spawn_handler_substitutes_json_path_into_handler_command(tmp_path: Path) -> None:
    """ProcessManager.spawn_handler must format {json_file} with the absolute path and run it."""
    output_file = tmp_path / "handler_output.txt"
    json_path = tmp_path / "test.json"
    json_path.write_text('{"content": "test", "name": "test"}', encoding="utf-8")

    process_manager = ProcessManager(f"echo {{json_file}} >> {output_file}")
    process_manager.spawn_handler("some-task", json_path)

    # spawn_handler runs the command asynchronously; wait on the tracked process so
    # the assertion is deterministic rather than racing the handler.
    handler = process_manager.active_handlers["some-task"]
    handler.wait(timeout=10)

    assert output_file.read_text().strip() == str(json_path.absolute())


def test_process_tasks_deletes_json_for_removed_task(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
task-one
    content one
task-two
    content two
""",
        encoding="utf-8",
    )

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    process_manager = ProcessManager("echo {json_file}")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
    assert task_names == {"task-one", "task-two"}
    assert (task_dir / "task-one.json").exists()
    assert (task_dir / "task-two.json").exists()

    task_file.write_text(
        """task:
task-one
    content one
""",
        encoding="utf-8",
    )

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
    assert task_names == {"task-one"}
    assert (task_dir / "task-one.json").exists()
    assert not (task_dir / "task-two.json").exists()


def test_process_tasks_initial_sync_does_not_delete_orphan_json(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
task-one
    content one
""",
        encoding="utf-8",
    )

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()

    orphan_json = task_dir / "old-task.json"
    write_json_file(orphan_json, {"content": "old content", "name": "old-task"})

    process_manager = ProcessManager("echo {json_file}")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)

    assert task_names == {"task-one"}
    assert (task_dir / "task-one.json").exists()
    assert orphan_json.exists()


def test_process_tasks_terminates_handler_for_removed_task(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
long-running-task
    content
""",
        encoding="utf-8",
    )

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()

    # Unique, long-lived duration so this spawned process can't collide with another
    # test's handler (per the globally-unique-constant rule) and outlasts the test.
    process_manager = ProcessManager("sleep 31607")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
    assert task_names == {"long-running-task"}

    json_path = task_dir / "long-running-task.json"
    process_manager.spawn_handler("long-running-task", json_path)

    assert "long-running-task" in process_manager.active_handlers
    handler_process = process_manager.active_handlers["long-running-task"]
    assert handler_process.poll() is None

    task_file.write_text("", encoding="utf-8")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
    assert task_names == set()

    assert "long-running-task" not in process_manager.active_handlers


def test_process_tasks_moves_markdown_files_for_deleted_task(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
task-one
    content one
task-two
    content two
""",
        encoding="utf-8",
    )

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()

    md_dir = tmp_path / "md"
    md_dir.mkdir()

    md_file_one_a = md_dir / "task-one_notes.md"
    md_file_one_b = md_dir / "task-one_details.md"
    md_file_two = md_dir / "task-two_info.md"

    md_file_one_a.write_text("Notes for task one", encoding="utf-8")
    md_file_one_b.write_text("Details for task one", encoding="utf-8")
    md_file_two.write_text("Info for task two", encoding="utf-8")

    process_manager = ProcessManager("echo {json_file}")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
    assert task_names == {"task-one", "task-two"}

    task_file.write_text(
        """task:
task-one
    content one
""",
        encoding="utf-8",
    )

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
    assert task_names == {"task-one"}

    md_done_dir = tmp_path / "md_done"
    assert md_done_dir.exists()
    assert not md_file_two.exists()
    assert (md_done_dir / "task-two_info.md").exists()

    assert md_file_one_a.exists()
    assert md_file_one_b.exists()

    moved_file = md_done_dir / "task-two_info.md"
    assert moved_file.read_text(encoding="utf-8") == "Info for task two"


def test_process_tasks_deletion_works_without_md_directory(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
task-one
    content one
""",
        encoding="utf-8",
    )

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()

    # Explicitly do NOT create the md directory.

    process_manager = ProcessManager("echo {json_file}")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
    assert task_names == {"task-one"}

    task_file.write_text("", encoding="utf-8")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
    assert task_names == set()

    assert not (task_dir / "task-one.json").exists()


def test_process_tasks_deletion_leaves_unrelated_markdown_untouched(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
task-one
    content one
""",
        encoding="utf-8",
    )

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()

    md_dir = tmp_path / "md"
    md_dir.mkdir()

    other_file = md_dir / "other_file.md"
    other_file.write_text("Some other content", encoding="utf-8")

    process_manager = ProcessManager("echo {json_file}")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
    assert task_names == {"task-one"}

    task_file.write_text("", encoding="utf-8")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
    assert task_names == set()

    assert not (task_dir / "task-one.json").exists()

    md_done_dir = tmp_path / "md_done"
    assert not md_done_dir.exists()
    assert other_file.exists()


def test_process_tasks_moves_all_matching_markdown_files_for_deleted_task(tmp_path: Path) -> None:
    task_file = tmp_path / "tasks.txt"
    task_file.write_text(
        """task:
complex-task
    content
""",
        encoding="utf-8",
    )

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()

    md_dir = tmp_path / "md"
    md_dir.mkdir()

    md_files = [
        md_dir / "complex-task_notes.md",
        md_dir / "complex-task_details.md",
        md_dir / "complex-task_summary.md",
        md_dir / "complex-task_references.md",
    ]

    for i, md_file in enumerate(md_files):
        md_file.write_text(f"Content {i}", encoding="utf-8")

    process_manager = ProcessManager("echo {json_file}")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
    assert task_names == {"complex-task"}

    task_file.write_text("", encoding="utf-8")

    task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
    assert task_names == set()

    md_done_dir = tmp_path / "md_done"
    assert md_done_dir.exists()

    for md_file in md_files:
        assert not md_file.exists()
        assert (md_done_dir / md_file.name).exists()

    for i, md_file in enumerate(md_files):
        assert (md_done_dir / md_file.name).read_text(encoding="utf-8") == f"Content {i}"
