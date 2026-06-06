#!/usr/bin/env python3
"""File-watching automation utility for task coordination.

Monitors a plain text file containing task definitions, parses the custom format
to extract individual tasks, synchronizes them to JSON files, and invokes a handler
script when tasks change.
"""

import json
import logging
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class Task:
    """Represents a parsed task with name and content."""

    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.content = content

    def to_dict(self) -> dict[str, str]:
        """Convert task to dictionary with sorted keys."""
        return {"content": self.content, "name": self.name}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Task):
            return False
        return self.name == other.name and self.content == other.content

    def __repr__(self) -> str:
        return f"Task(name={self.name!r}, content={self.content!r})"


def normalize_task_name(name: str) -> str:
    """Normalize a task name according to the spec.

    Algorithm:
    1. Strip leading/trailing whitespace
    2. Replace non-alphanumeric/non-hyphen characters with hyphen
    3. Collapse multiple consecutive hyphens to single hyphen
    4. Strip leading/trailing hyphens
    5. Convert to lowercase

    Args:
        name: Raw task name from input file

    Returns:
        Normalized task name suitable for use as filename

    Raises:
        ValueError: If normalized name is empty
    """
    # Strip whitespace
    name = name.strip()

    # Replace non-alphanumeric/non-hyphen with hyphen
    name = re.sub(r"[^a-zA-Z0-9-]", "-", name)

    # Collapse multiple hyphens
    name = re.sub(r"-+", "-", name)

    # Strip leading/trailing hyphens
    name = name.strip("-")

    # Convert to lowercase
    name = name.lower()

    if not name:
        raise ValueError("Task name normalized to empty string")

    return name


def parse_sections(content: str) -> dict[str, str]:
    """Parse content into sections.

    A section starts with 'section_name:' and continues until a blank line
    or end of file. Multiple sections with the same name are concatenated.

    Args:
        content: The full file content

    Returns:
        Dictionary mapping section names to their content (stripped)
    """
    sections: dict[str, list[str]] = {}
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check if this is a section header (line ending with ':')
        if line.strip() and line.rstrip().endswith(":"):
            # Extract section name (everything before the colon)
            section_name = line.rstrip()[:-1].rstrip()

            # Collect section content until blank line or EOF
            section_lines = []
            i += 1
            while i < len(lines):
                line = lines[i]
                # Check if this is a blank line (only whitespace or empty)
                if not line.strip():
                    break
                section_lines.append(line)
                i += 1

            # Store section content
            section_content = "\n".join(section_lines).strip()
            if section_name not in sections:
                sections[section_name] = []
            sections[section_name].append(section_content)
        else:
            i += 1

    # Concatenate multiple sections with same name
    return {name: "\n".join(content_list) for name, content_list in sections.items()}


def parse_tasks(task_section_content: str) -> list[Task]:
    """Parse the task section content into individual tasks.

    Task names are non-indented lines, task content is indented lines.
    Content indentation is reduced by 4 spaces (or all leading spaces if < 4).

    Args:
        task_section_content: The concatenated content of all task sections

    Returns:
        List of Task objects
    """
    if not task_section_content.strip():
        return []

    tasks: list[Task] = []
    lines = task_section_content.split("\n")

    current_task_name: str | None = None
    current_task_content_lines: list[str] = []

    for line in lines:
        # Check if line has leading spaces
        if line and not line[0].isspace():
            # This is a task name line
            # Save previous task if exists
            if current_task_name is not None:
                try:
                    normalized_name = normalize_task_name(current_task_name)
                    # Process content lines: remove first 4 spaces from each
                    processed_lines = []
                    for content_line in current_task_content_lines:
                        # Remove up to 4 leading spaces
                        spaces_to_remove = min(4, len(content_line) - len(content_line.lstrip(" ")))
                        processed_lines.append(content_line[spaces_to_remove:])

                    content = "\n".join(processed_lines).strip()
                    tasks.append(Task(name=normalized_name, content=content))
                except ValueError as e:
                    logger.error(f"Skipping task with invalid name '{current_task_name}': {e}")

            # Start new task
            current_task_name = line.strip()
            current_task_content_lines = []
        elif line:
            # This is a content line (has leading spaces)
            current_task_content_lines.append(line)

    # Don't forget the last task
    if current_task_name is not None:
        try:
            normalized_name = normalize_task_name(current_task_name)
            processed_lines = []
            for content_line in current_task_content_lines:
                spaces_to_remove = min(4, len(content_line) - len(content_line.lstrip(" ")))
                processed_lines.append(content_line[spaces_to_remove:])

            content = "\n".join(processed_lines).strip()
            tasks.append(Task(name=normalized_name, content=content))
        except ValueError as e:
            logger.error(f"Skipping task with invalid name '{current_task_name}': {e}")

    return tasks


def parse_task_file(file_path: Path) -> list[Task]:
    """Parse a task file and extract all tasks.

    Args:
        file_path: Path to the task file

    Returns:
        List of Task objects

    Raises:
        OSError: If file cannot be read
    """
    content = file_path.read_text(encoding="utf-8")
    sections = parse_sections(content)

    # Get task section content (or empty string if not present)
    task_section_content = sections.get("task", "")

    return parse_tasks(task_section_content)


def read_json_file(file_path: Path) -> dict[str, str] | None:
    """Read and parse a JSON file.

    Args:
        file_path: Path to JSON file

    Returns:
        Parsed JSON as dict, or None if file doesn't exist or is malformed
    """
    if not file_path.exists():
        return None

    try:
        content = file_path.read_text(encoding="utf-8")
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read/parse {file_path}: {e}")
        return None


def write_json_file(file_path: Path, data: dict[str, str]) -> None:
    """Write data to JSON file atomically.

    Args:
        file_path: Path to JSON file
        data: Data to write

    Raises:
        OSError: If file cannot be written
    """
    # Serialize with sorted keys and 2-space indentation
    json_content = json.dumps(data, sort_keys=True, indent=2)
    # Ensure trailing newline
    if not json_content.endswith("\n"):
        json_content += "\n"

    # Write atomically using temp file
    temp_path = file_path.with_suffix(".tmp")
    try:
        temp_path.write_text(json_content, encoding="utf-8")
        temp_path.replace(file_path)
    except OSError:
        # Clean up temp file if it exists
        if temp_path.exists():
            temp_path.unlink()
        raise


class ProcessManager:
    """Manages handler processes for tasks."""

    def __init__(self, handler_command: str) -> None:
        self.handler_command = handler_command
        self.active_handlers: dict[str, subprocess.Popen] = {}

    def terminate_handler(self, task_name: str) -> None:
        """Terminate the handler process for a task.

        Sends SIGTERM, waits 5 seconds, then sends SIGKILL if needed.

        Args:
            task_name: Name of task whose handler to terminate
        """
        if task_name not in self.active_handlers:
            return

        process = self.active_handlers[task_name]

        # Check if already terminated
        if process.poll() is not None:
            del self.active_handlers[task_name]
            return

        # Send SIGTERM
        logger.info(f"Terminating handler for task '{task_name}' (PID {process.pid})")
        try:
            process.terminate()
        except ProcessLookupError:
            # Process already terminated
            del self.active_handlers[task_name]
            return

        # Wait up to 5 seconds
        try:
            process.wait(timeout=5.0)
            logger.info(f"Handler for task '{task_name}' terminated gracefully")
        except subprocess.TimeoutExpired:
            # Send SIGKILL
            logger.warning(f"Handler for task '{task_name}' did not terminate, sending SIGKILL")
            try:
                process.kill()
                process.wait()
            except ProcessLookupError:
                pass

        del self.active_handlers[task_name]

    def spawn_handler(self, task_name: str, json_path: Path) -> None:
        """Spawn a handler process for a task.

        Terminates any existing handler for this task first.

        Args:
            task_name: Name of the task
            json_path: Path to the task's JSON file
        """
        # Terminate previous handler if exists
        self.terminate_handler(task_name)

        # Format the command with the json file path
        command = self.handler_command.format(json_file=str(json_path.absolute()))

        # Spawn new handler
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=None,  # Inherit from parent
                stderr=None,  # Inherit from parent
            )
            self.active_handlers[task_name] = process
            logger.info(f"Spawned handler for task '{task_name}' (PID {process.pid})")
        except OSError as e:
            logger.error(f"Failed to spawn handler for task '{task_name}': {e}")

    def terminate_all(self) -> None:
        """Terminate all active handlers."""
        logger.info("Terminating all active handlers")
        task_names = list(self.active_handlers.keys())
        for task_name in task_names:
            self.terminate_handler(task_name)


def process_tasks(
    watch_file: Path,
    task_dir: Path,
    process_manager: ProcessManager,
    previous_task_names: set[str] | None = None,
) -> set[str]:
    """Parse task file and sync tasks to JSON files.

    Args:
        watch_file: Path to the task file to parse
        task_dir: Directory where task JSON files are stored
        process_manager: Manager for handler processes
        previous_task_names: Set of task names from previous parse, or None for initial sync

    Returns:
        Set of task names from this parse
    """
    try:
        tasks = parse_task_file(watch_file)
        logger.info(f"Parsed {len(tasks)} tasks")

        current_task_names = set()

        for task in tasks:
            current_task_names.add(task.name)
            json_path = task_dir / f"{task.name}.json"
            task_dict = task.to_dict()

            # Check if update needed
            existing_data = read_json_file(json_path)
            if existing_data == task_dict:
                # No change
                continue

            # Write updated JSON
            try:
                write_json_file(json_path, task_dict)
                logger.info(f"Updated task file: {json_path}")

                # Invoke handler
                process_manager.spawn_handler(task.name, json_path)
            except OSError as e:
                logger.error(f"Failed to write {json_path}: {e}")

        # Handle deleted tasks (but skip during initial sync)
        if previous_task_names is not None:
            deleted_tasks = previous_task_names - current_task_names
            for task_name in deleted_tasks:
                logger.info(f"Task '{task_name}' was removed, cleaning up")

                # Terminate handler if running
                process_manager.terminate_handler(task_name)

                # Delete JSON file
                json_path = task_dir / f"{task_name}.json"
                try:
                    if json_path.exists():
                        json_path.unlink()
                        logger.info(f"Deleted JSON file: {json_path}")
                except OSError as e:
                    logger.error(f"Failed to delete {json_path}: {e}")

                # Move markdown files from md/ to md_done/
                md_dir = task_dir.parent / "md"
                md_done_dir = task_dir.parent / "md_done"

                if md_dir.exists():
                    try:
                        # Find all markdown files matching the pattern <task_name>_*.md
                        md_pattern = f"{task_name}_*.md"
                        md_files = list(md_dir.glob(md_pattern))

                        if md_files:
                            # Create md_done directory if it doesn't exist
                            md_done_dir.mkdir(parents=True, exist_ok=True)

                            # Move each markdown file
                            for md_file in md_files:
                                dest_path = md_done_dir / md_file.name
                                try:
                                    md_file.rename(dest_path)
                                    logger.info(f"Moved markdown file: {md_file} -> {dest_path}")
                                except OSError as e:
                                    logger.error(f"Failed to move {md_file} to {dest_path}: {e}")
                    except OSError as e:
                        logger.error(f"Error accessing markdown directory {md_dir}: {e}")

        return current_task_names

    except OSError as e:
        logger.error(f"Failed to read task file: {e}")
        return set()
    except Exception as e:
        logger.error(f"Error processing tasks: {e}")
        return set()


@click.command()
@click.argument("watch_file", type=click.Path(exists=True, path_type=Path))
@click.argument("task_dir", type=click.Path(path_type=Path))
@click.argument("handler_command")
def main(watch_file: Path, task_dir: Path, handler_command: str) -> None:
    """Watch a plain text task file and synchronize tasks to JSON files.

    When WATCH_FILE changes, parse it to extract tasks, write each task to a
    JSON file in TASK_DIR, and invoke HANDLER_COMMAND with the path to any
    changed task file.

    The HANDLER_COMMAND is treated as a bash command string. Use {json_file}
    as a placeholder for the JSON file path, which will be expanded when invoked.

    \b
    Arguments:
      WATCH_FILE       Path to the plain text file to watch for changes
      TASK_DIR         Directory where task JSON files will be stored
      HANDLER_COMMAND  Bash command to run when a task is updated.
                       Use {json_file} for the JSON file path.

    \b
    Examples:
      coordinator.py tasks.txt ./task_jsons "python process.py {json_file}"
      coordinator.py tasks.txt ./task_jsons "echo {json_file} >> log.txt"
    """

    # Create task directory if needed
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"Failed to create task directory: {e}")
        sys.exit(1)

    logger.info("Starting coordinator")
    logger.info(f"  Watch file: {watch_file}")
    logger.info(f"  Task directory: {task_dir}")
    logger.info(f"  Handler command: {handler_command}")

    # Initialize process manager
    process_manager = ProcessManager(handler_command)

    # Setup signal handlers for graceful shutdown
    shutdown_requested = False

    def signal_handler(signum, frame):
        nonlocal shutdown_requested
        logger.info(f"Received signal {signum}, shutting down")
        shutdown_requested = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Perform initial sync (don't delete tasks on first run)
    logger.info("Performing initial sync")
    current_task_names = process_tasks(watch_file, task_dir, process_manager, previous_task_names=None)

    # Track last modification time
    last_mtime = watch_file.stat().st_mtime

    logger.info("Watching for changes (Ctrl+C to stop)")

    # Poll for file changes
    try:
        while not shutdown_requested:
            time.sleep(1)

            # Check if file was modified
            try:
                current_mtime = watch_file.stat().st_mtime
                if current_mtime != last_mtime:
                    logger.info(f"Detected change in {watch_file}")
                    last_mtime = current_mtime
                    # Pass previous task names to enable deletion detection
                    current_task_names = process_tasks(
                        watch_file,
                        task_dir,
                        process_manager,
                        previous_task_names=current_task_names,
                    )
            except OSError as e:
                logger.error(f"Error checking file: {e}")

    except KeyboardInterrupt:
        logger.info("Stopping coordinator")

    process_manager.terminate_all()


if __name__ == "__main__":
    main()
