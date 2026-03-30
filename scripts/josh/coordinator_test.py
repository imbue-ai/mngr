"""Tests for the coordinator script."""

import subprocess
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


class TestNormalizeTaskName:
    """Tests for task name normalization."""

    def test_already_normalized(self):
        """Test name that's already in correct format."""
        assert normalize_task_name("first-task") == "first-task"

    def test_spaces_to_hyphens(self):
        """Test that spaces are converted to hyphens."""
        assert normalize_task_name("even easier task") == "even-easier-task"

    def test_long_name_with_spaces(self):
        """Test normalization of long names with spaces."""
        assert (
            normalize_task_name("another task to demonstrate that spaces are ok in task names")
            == "another-task-to-demonstrate-that-spaces-are-ok-in-task-names"
        )

    def test_mixed_case_to_lowercase(self):
        """Test that mixed case is converted to lowercase."""
        assert normalize_task_name("a final task I forgot") == "a-final-task-i-forgot"
        assert normalize_task_name("Task #1!") == "task-1"

    def test_special_characters_to_hyphens(self):
        """Test that special characters are converted to hyphens."""
        assert normalize_task_name("task#1!") == "task-1"
        assert normalize_task_name("my@task$name") == "my-task-name"

    def test_multiple_hyphens_collapsed(self):
        """Test that multiple consecutive hyphens are collapsed."""
        assert normalize_task_name("task---with---hyphens") == "task-with-hyphens"
        assert normalize_task_name("task   with   spaces") == "task-with-spaces"

    def test_leading_trailing_hyphens_stripped(self):
        """Test that leading/trailing hyphens are removed."""
        assert normalize_task_name("-task-") == "task"
        assert normalize_task_name("---task---") == "task"

    def test_empty_name_raises_error(self):
        """Test that normalizing to empty string raises ValueError."""
        with pytest.raises(ValueError, match="empty string"):
            normalize_task_name("!!!")
        with pytest.raises(ValueError, match="empty string"):
            normalize_task_name("---")
        with pytest.raises(ValueError, match="empty string"):
            normalize_task_name("   ")


class TestParseSections:
    """Tests for section parsing."""

    def test_single_section(self):
        """Test parsing a single section."""
        content = """goal:
demonstrate this format

"""
        sections = parse_sections(content)
        assert sections == {"goal": "demonstrate this format"}

    def test_multiple_sections(self):
        """Test parsing multiple different sections."""
        content = """goal:
first section

reminder:
second section
"""
        sections = parse_sections(content)
        assert sections == {
            "goal": "first section",
            "reminder": "second section",
        }

    def test_multiple_instances_of_same_section(self):
        """Test that multiple instances of same section are concatenated."""
        content = """foo:
first content

bar:
middle section

foo:
second content
"""
        sections = parse_sections(content)
        assert sections == {
            "foo": "first content\nsecond content",
            "bar": "middle section",
        }

    def test_section_with_internal_content(self):
        """Test section with multi-line content."""
        content = """reminder:
pay attention to all of the instructions!
    there might be details
and it's important to get everything right

"""
        sections = parse_sections(content)
        expected = "pay attention to all of the instructions!\n    there might be details\nand it's important to get everything right"
        assert sections["reminder"] == expected

    def test_empty_content(self):
        """Test parsing empty file."""
        sections = parse_sections("")
        assert sections == {}

    def test_no_sections(self):
        """Test parsing content with no sections."""
        content = """just some text
without any sections
"""
        sections = parse_sections(content)
        assert sections == {}

    def test_section_at_end_of_file(self):
        """Test section that ends at EOF without blank line."""
        content = """task:
some task"""
        sections = parse_sections(content)
        assert sections == {"task": "some task"}


class TestParseTasks:
    """Tests for task extraction from task section."""

    def test_single_task_with_content(self):
        """Test parsing a single task with content."""
        task_content = """first-task
    do something easy"""
        tasks = parse_tasks(task_content)
        assert len(tasks) == 1
        assert tasks[0].name == "first-task"
        assert tasks[0].content == "do something easy"

    def test_task_without_content(self):
        """Test task with no content lines."""
        task_content = "even-easier-task-with-no-description"
        tasks = parse_tasks(task_content)
        assert len(tasks) == 1
        assert tasks[0].name == "even-easier-task-with-no-description"
        assert tasks[0].content == ""

    def test_multiple_tasks(self):
        """Test parsing multiple tasks."""
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

    def test_task_with_nested_indentation(self):
        """Test task with various levels of indentation."""
        task_content = """another task to demonstrate that spaces are ok in task names
    and obviously
        there can be
            lots of indentation
    but remember to remove only the first 4 spaces"""
        tasks = parse_tasks(task_content)
        assert len(tasks) == 1
        assert tasks[0].name == "another-task-to-demonstrate-that-spaces-are-ok-in-task-names"
        expected_content = "and obviously\n    there can be\n        lots of indentation\nbut remember to remove only the first 4 spaces"
        assert tasks[0].content == expected_content

    def test_empty_task_section(self):
        """Test parsing empty task section."""
        tasks = parse_tasks("")
        assert tasks == []

    def test_complete_example_from_spec(self):
        """Test the complete example from the specification."""
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
        expected = "and obviously\n    there can be\n        lots of indentation\nbut remember to remove only the first 4 spaces"
        assert tasks[2].content == expected

        assert tasks[3].name == "a-final-task-i-forgot"
        assert tasks[3].content == "with some details\nand text"

    def test_task_with_invalid_name_skipped(self):
        """Test that tasks with invalid names are skipped."""
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


class TestParseTaskFile:
    """Tests for parsing complete task files."""

    def test_parse_complete_example(self, tmp_path: Path):
        """Test parsing the complete example from spec."""
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


class TestTask:
    """Tests for Task class."""

    def test_to_dict(self):
        """Test conversion to dictionary with sorted keys."""
        task = Task(name="test-task", content="test content")
        task_dict = task.to_dict()
        assert task_dict == {"content": "test content", "name": "test-task"}
        # Verify keys are in sorted order
        assert list(task_dict.keys()) == ["content", "name"]

    def test_equality(self):
        """Test task equality."""
        task1 = Task(name="task", content="content")
        task2 = Task(name="task", content="content")
        task3 = Task(name="task", content="different")
        task4 = Task(name="different", content="content")

        assert task1 == task2
        assert task1 != task3
        assert task1 != task4
        assert task1 != "not a task"


class TestJSONOperations:
    """Tests for JSON file operations."""

    def test_write_and_read_json(self, tmp_path: Path):
        """Test writing and reading JSON files."""
        json_file = tmp_path / "test.json"
        data = {"content": "test content", "name": "test-task"}

        write_json_file(json_file, data)
        assert json_file.exists()

        read_data = read_json_file(json_file)
        assert read_data == data

    def test_json_format(self, tmp_path: Path):
        """Test that JSON is formatted correctly."""
        json_file = tmp_path / "test.json"
        data = {"content": "test", "name": "task"}

        write_json_file(json_file, data)

        content = json_file.read_text(encoding="utf-8")
        # Should have 2-space indentation and sorted keys
        expected = '{\n  "content": "test",\n  "name": "task"\n}\n'
        assert content == expected

    def test_read_nonexistent_file(self, tmp_path: Path):
        """Test reading file that doesn't exist."""
        json_file = tmp_path / "nonexistent.json"
        result = read_json_file(json_file)
        assert result is None

    def test_read_malformed_json(self, tmp_path: Path):
        """Test reading malformed JSON file."""
        json_file = tmp_path / "malformed.json"
        json_file.write_text("not valid json", encoding="utf-8")

        result = read_json_file(json_file)
        assert result is None


class TestIntegration:
    """Integration tests for the coordinator script."""

    def test_initial_sync_creates_json_files(self, tmp_path: Path):
        """Test that initial sync creates JSON files for tasks."""
        # Create task file
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

        # Create task directory
        task_dir = tmp_path / "tasks"
        task_dir.mkdir()

        # Parse and process tasks manually (simulating what coordinator does)
        tasks = parse_task_file(task_file)
        for task in tasks:
            json_path = task_dir / f"{task.name}.json"
            write_json_file(json_path, task.to_dict())

        # Verify JSON files created
        assert (task_dir / "task-one.json").exists()
        assert (task_dir / "task-two.json").exists()

        # Verify content
        task_one_data = read_json_file(task_dir / "task-one.json")
        assert task_one_data == {"content": "content for task one", "name": "task-one"}

    def test_update_detection(self, tmp_path: Path):
        """Test that changes to tasks are detected."""
        task_dir = tmp_path / "tasks"
        task_dir.mkdir()

        # Create initial JSON file
        json_path = task_dir / "test-task.json"
        initial_data = {"content": "initial content", "name": "test-task"}
        write_json_file(json_path, initial_data)

        # Verify no update needed for same content
        existing = read_json_file(json_path)
        assert existing == initial_data

        # Update with different content
        updated_data = {"content": "updated content", "name": "test-task"}
        write_json_file(json_path, updated_data)

        # Verify update detected
        existing = read_json_file(json_path)
        assert existing == updated_data
        assert existing != initial_data

    def test_handler_invocation(self, tmp_path: Path):
        """Test that handler command can be invoked with format string."""
        # Create output file for handler
        output_file = tmp_path / "handler_output.txt"

        # Create test JSON file
        json_file = tmp_path / "test.json"
        json_file.write_text('{"content": "test", "name": "test"}', encoding="utf-8")

        # Create handler command that writes to a file
        handler_command = f'echo "{{json_file}}" >> {output_file}'
        formatted_command = handler_command.format(json_file=str(json_file))

        # Invoke handler
        subprocess.run(formatted_command, shell=True, check=True)

        # Verify handler was invoked
        assert output_file.exists()
        content = output_file.read_text().strip()
        assert str(json_file) in content

    def test_task_deletion(self, tmp_path: Path):
        """Test that removed tasks have their JSON files deleted."""
        # Create initial task file with two tasks
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

        # Create a dummy handler command
        handler_command = "echo {json_file}"
        process_manager = ProcessManager(handler_command)

        # Initial sync - should create both tasks
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
        assert task_names == {"task-one", "task-two"}
        assert (task_dir / "task-one.json").exists()
        assert (task_dir / "task-two.json").exists()

        # Update file to remove task-two
        task_file.write_text(
            """task:
task-one
    content one
""",
            encoding="utf-8",
        )

        # Process with previous task names - should delete task-two
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
        assert task_names == {"task-one"}
        assert (task_dir / "task-one.json").exists()
        assert not (task_dir / "task-two.json").exists()

    def test_no_deletion_on_initial_sync(self, tmp_path: Path):
        """Test that existing JSON files are not deleted during initial sync."""
        # Create task file with one task
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

        # Create a pre-existing JSON file for a different task
        orphan_json = task_dir / "old-task.json"
        write_json_file(orphan_json, {"content": "old content", "name": "old-task"})

        # Create a dummy handler command
        handler_command = "echo {json_file}"
        process_manager = ProcessManager(handler_command)

        # Initial sync with previous_task_names=None
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)

        # Should create task-one but NOT delete old-task
        assert task_names == {"task-one"}
        assert (task_dir / "task-one.json").exists()
        assert orphan_json.exists()  # Should still exist

    def test_handler_terminated_on_deletion(self, tmp_path: Path):
        """Test that handlers are terminated when tasks are deleted."""
        # Create task file
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

        # Create a long-running handler command
        handler_command = "sleep 100"
        process_manager = ProcessManager(handler_command)

        # Initial sync
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
        assert task_names == {"long-running-task"}

        # Spawn a handler for the task
        json_path = task_dir / "long-running-task.json"
        process_manager.spawn_handler("long-running-task", json_path)

        # Verify handler is running
        assert "long-running-task" in process_manager.active_handlers
        handler_process = process_manager.active_handlers["long-running-task"]
        assert handler_process.poll() is None  # Still running

        # Update file to remove the task
        task_file.write_text("", encoding="utf-8")

        # Process with deletion - should terminate handler
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
        assert task_names == set()

        # Handler should be terminated
        assert "long-running-task" not in process_manager.active_handlers

    def test_markdown_files_moved_on_deletion(self, tmp_path: Path):
        """Test that markdown files are moved to md_done when tasks are deleted."""
        # Create initial task file with two tasks
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

        # Create md directory with markdown files for both tasks
        md_dir = tmp_path / "md"
        md_dir.mkdir()

        # Create markdown files following the pattern <task_name>_*.md
        md_file_one_a = md_dir / "task-one_notes.md"
        md_file_one_b = md_dir / "task-one_details.md"
        md_file_two = md_dir / "task-two_info.md"

        md_file_one_a.write_text("Notes for task one", encoding="utf-8")
        md_file_one_b.write_text("Details for task one", encoding="utf-8")
        md_file_two.write_text("Info for task two", encoding="utf-8")

        # Create a dummy handler command
        handler_command = "echo {json_file}"
        process_manager = ProcessManager(handler_command)

        # Initial sync - should create both tasks
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
        assert task_names == {"task-one", "task-two"}

        # Update file to remove task-two
        task_file.write_text(
            """task:
task-one
    content one
""",
            encoding="utf-8",
        )

        # Process with deletion
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
        assert task_names == {"task-one"}

        # Verify task-two markdown file was moved to md_done
        md_done_dir = tmp_path / "md_done"
        assert md_done_dir.exists()
        assert not md_file_two.exists()  # Should be moved
        assert (md_done_dir / "task-two_info.md").exists()  # Should be in md_done

        # Verify task-one markdown files are still in md (not moved)
        assert md_file_one_a.exists()
        assert md_file_one_b.exists()

        # Verify content is preserved
        moved_file = md_done_dir / "task-two_info.md"
        assert moved_file.read_text(encoding="utf-8") == "Info for task two"

    def test_markdown_deletion_with_no_md_directory(self, tmp_path: Path):
        """Test that task deletion works gracefully when md directory doesn't exist."""
        # Create initial task file
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

        # Explicitly do NOT create md directory

        # Create a dummy handler command
        handler_command = "echo {json_file}"
        process_manager = ProcessManager(handler_command)

        # Initial sync
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
        assert task_names == {"task-one"}

        # Update file to remove task-one
        task_file.write_text("", encoding="utf-8")

        # Process with deletion - should not crash even without md directory
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
        assert task_names == set()

        # Verify JSON was deleted
        assert not (task_dir / "task-one.json").exists()

    def test_markdown_deletion_with_no_matching_files(self, tmp_path: Path):
        """Test task deletion when no matching markdown files exist."""
        # Create initial task file
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

        # Create md directory but no markdown files for task-one
        md_dir = tmp_path / "md"
        md_dir.mkdir()

        # Create a markdown file that doesn't match the pattern
        other_file = md_dir / "other_file.md"
        other_file.write_text("Some other content", encoding="utf-8")

        # Create a dummy handler command
        handler_command = "echo {json_file}"
        process_manager = ProcessManager(handler_command)

        # Initial sync
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
        assert task_names == {"task-one"}

        # Update file to remove task-one
        task_file.write_text("", encoding="utf-8")

        # Process with deletion
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
        assert task_names == set()

        # Verify JSON was deleted
        assert not (task_dir / "task-one.json").exists()

        # Verify md_done was NOT created (no files to move)
        md_done_dir = tmp_path / "md_done"
        assert not md_done_dir.exists()

        # Verify the other file is still there
        assert other_file.exists()

    def test_multiple_markdown_files_moved(self, tmp_path: Path):
        """Test that all matching markdown files are moved for a deleted task."""
        # Create initial task file
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

        # Create md directory with multiple markdown files for the task
        md_dir = tmp_path / "md"
        md_dir.mkdir()

        # Create several markdown files for the same task
        md_files = [
            md_dir / "complex-task_notes.md",
            md_dir / "complex-task_details.md",
            md_dir / "complex-task_summary.md",
            md_dir / "complex-task_references.md",
        ]

        for i, md_file in enumerate(md_files):
            md_file.write_text(f"Content {i}", encoding="utf-8")

        # Create a dummy handler command
        handler_command = "echo {json_file}"
        process_manager = ProcessManager(handler_command)

        # Initial sync
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=None)
        assert task_names == {"complex-task"}

        # Update file to remove the task
        task_file.write_text("", encoding="utf-8")

        # Process with deletion
        task_names = process_tasks(task_file, task_dir, process_manager, previous_task_names=task_names)
        assert task_names == set()

        # Verify all markdown files were moved
        md_done_dir = tmp_path / "md_done"
        assert md_done_dir.exists()

        for md_file in md_files:
            assert not md_file.exists()  # Should be moved from md
            moved_file = md_done_dir / md_file.name
            assert moved_file.exists()  # Should be in md_done

        # Verify content is preserved
        for i, md_file in enumerate(md_files):
            moved_file = md_done_dir / md_file.name
            assert moved_file.read_text(encoding="utf-8") == f"Content {i}"
