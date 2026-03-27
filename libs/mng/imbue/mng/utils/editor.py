import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any
from typing import Final
from typing import Self

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.errors import UserInputError
from imbue.mng.utils.interactive_subprocess import popen_interactive_subprocess

FALLBACK_EDITORS: Final[tuple[str, ...]] = ("vim", "vi", "nano", "notepad")


def get_editor_command() -> str:
    """Get the editor command from environment variables or use a fallback.

    Checks $VISUAL first (for full-screen editors), then $EDITOR,
    then falls back to common editors.
    """
    # Check VISUAL first (preferred for interactive editors)
    editor = os.environ.get("VISUAL")
    if editor:
        return editor

    # Check EDITOR next
    editor = os.environ.get("EDITOR")
    if editor:
        return editor

    # Try to find a fallback editor
    for fallback in FALLBACK_EDITORS:
        if shutil.which(fallback) is not None:
            return fallback

    # Last resort: just try vim
    return "vim"


class EditorSession:
    """Manages an interactive editor session for message editing.

    The editor runs in a subprocess while allowing other work to continue.
    The result is retrieved when wait_for_result() is called.

    Use the create() factory method to instantiate.
    """

    # Class attributes with type hints (not instance attributes)
    temp_file_path: Path
    editor_command: str
    _process: subprocess.Popen[Any] | None
    _is_started: bool
    _is_finished: bool
    _result_content: str | None
    _exit_code: int | None
    _exit_callback: Callable[[], None] | None
    _monitor_thread: threading.Thread | None
    _callback_called: bool
    _read_lock: threading.Lock
    _result_ready: threading.Event

    @classmethod
    def create(cls, initial_content: str | None = None) -> "EditorSession":
        """Create a new editor session with optional initial content."""
        # Create a temp file with the initial content
        temp_fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="mng-message-")
        temp_file_path = Path(temp_path)

        if initial_content:
            temp_file_path.write_text(initial_content)
        else:
            # Write empty file
            temp_file_path.write_text("")

        # Close the file descriptor (we'll access via path)
        os.close(temp_fd)

        editor_command = get_editor_command()
        logger.debug("Got editor command: {}", editor_command)

        # Create instance using object.__new__ and set attributes directly
        instance = object.__new__(cls)
        instance.temp_file_path = temp_file_path
        instance.editor_command = editor_command
        instance._process = None
        instance._is_started = False
        instance._is_finished = False
        instance._result_content = None
        instance._exit_code = None
        instance._exit_callback = None
        instance._monitor_thread = None
        instance._callback_called = False
        instance._read_lock = threading.Lock()
        instance._result_ready = threading.Event()
        return instance

    def start(self, on_exit: Callable[[], None] | None = None) -> None:
        """Start the editor subprocess.

        The editor process inherits stdin/stdout/stderr from the parent,
        giving it full terminal access.

        If on_exit is provided, a background thread will monitor the editor
        process and call the callback as soon as the editor exits. This is
        useful for restoring logging output immediately when the editor closes.
        """
        if self._is_started:
            raise UserInputError("Editor session already started")

        with log_span("Starting editor {} with file {}", self.editor_command, self.temp_file_path):
            # Start the editor process
            # The editor inherits the terminal (stdin/stdout/stderr) from parent
            self._process = popen_interactive_subprocess(
                [self.editor_command, str(self.temp_file_path)],
                stdin=None,
                stdout=None,
                stderr=None,
            )
        self._is_started = True
        self._exit_callback = on_exit
        logger.trace("Started editor process with PID {}", self._process.pid)

        # Start monitor thread if callback provided
        if on_exit is not None:
            self._monitor_thread = threading.Thread(
                target=self._monitor_process,
                daemon=True,
                name="editor-monitor",
            )
            self._monitor_thread.start()
            logger.trace("Started editor monitor thread")

    def _monitor_process(self) -> None:
        """Background thread that monitors the editor process and calls the exit callback."""
        if self._process is None:
            return

        # Wait for the process to exit
        self._process.wait()

        # Read the result immediately so it's available
        self._read_result()

        # Call the callback if we haven't already
        if self._exit_callback is not None and not self._callback_called:
            self._callback_called = True
            logger.trace("Detected editor exit, calling exit callback")
            # Call the callback without catching exceptions - let them propagate
            # The callback is expected to handle its own errors
            self._exit_callback()

    def _read_result(self) -> None:
        """Read the editor result from the temp file. Called by monitor thread on exit.

        Thread-safe: Uses a lock to ensure only one thread reads the file, and
        signals an event when the result is ready for other threads waiting.
        """
        if self._process is None:
            return

        # Use lock to ensure only one thread reads the result
        with self._read_lock:
            # Check if already read by another thread
            if self._is_finished:
                return

            self._exit_code = self._process.returncode
            logger.trace("Detected editor exit with code {}", self._exit_code)

            # Check exit code
            if self._exit_code != 0:
                logger.warning("Editor exited with non-zero code: {}", self._exit_code)
                self._is_finished = True
                self._result_ready.set()
                return

            # Read the edited content
            if not self.temp_file_path.exists():
                logger.debug("Editor temp file no longer exists")
                self._is_finished = True
                self._result_ready.set()
                return

            content = self.temp_file_path.read_text()

            # Strip trailing whitespace but preserve intentional content
            self._result_content = content.rstrip()

            if not self._result_content:
                logger.debug("Editor content is empty")
                self._result_content = None
            else:
                logger.trace("Read {} characters from edited file", len(self._result_content))

            # Mark as finished and signal waiting threads
            self._is_finished = True
            self._result_ready.set()

    def is_running(self) -> bool:
        """Check if the editor process is still running."""
        if not self._is_started or self._process is None:
            return False
        if self._is_finished:
            return False
        # Poll to check if process has finished
        return self._process.poll() is None

    def is_finished(self) -> bool:
        """Check if the editor session has finished (successfully or not)."""
        return self._is_finished

    def wait_for_result(self, timeout_seconds: float | None = None) -> str | None:
        """Wait for the editor to finish and return the edited content.

        Returns the content of the edited file, or None if:
        - The editor exited with a non-zero code
        - The file was empty after editing

        If the monitor thread has already processed the result (when on_exit
        callback was provided), this returns immediately with the cached result.
        """
        if not self._is_started or self._process is None:
            raise UserInputError("Editor session not started")

        # If result is already ready, return it immediately
        if self._result_ready.is_set():
            return self._result_content

        with log_span("Waiting for editor to finish..."):
            # Wait for the editor process to complete
            try:
                self._process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.warning("Editor timeout expired, terminating")
                self._process.terminate()
                self._process.wait()
                self._exit_code = -1
                self._is_finished = True
                self._result_ready.set()
                return None

            # Read result if not already done by monitor thread
            # This also handles the case where no monitor thread was started
            self._read_result()

            # Wait for the result to be fully ready (handles race with monitor thread)
            self._result_ready.wait(timeout=1.0)

        return self._result_content

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Clean up the temporary file and terminate the editor process if running.

        Should be called when done with the session, regardless of outcome.
        """
        # Terminate the editor process if it's still running
        if self._process is not None and self._process.poll() is None:
            with log_span("Terminating editor process"):
                self._process.terminate()
                try:
                    self._process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    logger.warning("Editor process did not terminate gracefully, killing")
                    self._process.kill()
                    self._process.wait()

        # Clean up the temp file
        if self.temp_file_path.exists():
            self.temp_file_path.unlink()
            logger.trace("Cleaned up temp file {}", self.temp_file_path)
