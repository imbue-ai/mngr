import contextlib
from io import StringIO
from typing import Any
from typing import Generator
from typing import Sequence

import modal
from loguru import logger
from modal._output import OutputManager

from imbue.imbue_common.logging import log_span
from imbue.mng.primitives import LogLevel
from imbue.mng.utils.logging import register_build_level

# Ensure BUILD level is registered (in case this module is imported before logging.py)
register_build_level()


def _write_to_multiple_files(
    files: Sequence[Any],
    text: str,
) -> int:
    """Write text to multiple file-like objects and return the length."""
    for file in files:
        file.write(text)
        file.flush()
    return len(text)


class _MultiWriter:
    """File-like object that writes to multiple destinations.

    This is used to tee Modal output to multiple destinations (e.g., a buffer
    for programmatic inspection and loguru for logging).
    """

    _files: Sequence[Any] = ()

    def write(self, text: str) -> int:
        """Write text to all configured file-like objects."""
        return _write_to_multiple_files(self._files, text)

    def flush(self) -> None:
        """Flush all file-like objects."""
        for file in self._files:
            file.flush()

    def isatty(self) -> bool:
        """Report as not a tty to disable interactive features."""
        return False

    def __enter__(self) -> "_MultiWriter":
        """Enter context."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Exit context."""
        pass


def _create_multi_writer(files: Sequence[Any]) -> _MultiWriter:
    """Create a new multi-writer that writes to all provided files."""
    writer = _MultiWriter()
    writer._files = files
    return writer


class ModalLoguruWriter:
    """Writer that sends Modal output to loguru with structured metadata.

    Supports setting app_id and app_name for structured logging.
    """

    app_id: str | None = None
    app_name: str | None = None
    current_line: str = ""

    def write(self, text: str) -> int:
        """Write text to loguru, deduplicating consecutive identical messages."""
        # stripped = text.strip()
        if text.strip() == "":
            return len(text)
        self.current_line += text
        if not self.current_line.endswith("\n"):
            return len(text)
        text_to_log = self.current_line.strip()
        self.current_line = ""
        try:
            logger.log(
                LogLevel.BUILD.value, "{}", text_to_log, source="modal", app_id=self.app_id, app_name=self.app_name
            )
        except ValueError as e:
            if "I/O operation on closed file" in str(e):
                pass
            else:
                raise
        return len(text)

    def flush(self) -> None:
        """Flush is a no-op for loguru."""
        pass

    def writable(self) -> bool:
        """Report as writable."""
        return True

    def readable(self) -> bool:
        """Report as not readable."""
        return False

    def seekable(self) -> bool:
        """Report as not seekable."""
        return False


def _create_modal_loguru_writer() -> ModalLoguruWriter:
    """Create a new Modal loguru writer instance."""
    writer = ModalLoguruWriter()
    writer.app_id = None
    writer.app_name = None
    return writer


@contextlib.contextmanager
def Pointless():
    yield None


class _QuietOutputManager(OutputManager):
    """Modal OutputManager that suppresses interactive spinners, status updates, and duplicate logs.

    Modal's default OutputManager displays spinners and progress bars which don't
    work well when capturing output programmatically. This subclass disables those
    features while preserving the ability to capture log output via _stdout.

    We use the timestamp of each log entry to deduplicate logs, as Modal sometimes
    emits the same log line multiple times during image builds.
    """

    _timestamps: set[float]

    @contextlib.contextmanager
    def show_status_spinner(self) -> Generator[None, None, None]:
        """Suppress the status spinner."""
        yield

    @staticmethod
    def step_progress(text: str = ""):
        return ""

    def make_live(self, renderable):
        return Pointless()

    # this captures the normal log lines from modal
    # If you want to get the modal URL for the app, it is logged here in one of the earlier messages
    def print(self, renderable) -> None:
        pass

    # this is where the build logs end up
    async def put_log_content(self, log):
        if log.timestamp not in self._timestamps:
            self._timestamps.add(log.timestamp)
            # print(log)
            self._stdout.write(log.data)

    def update_app_page_url(self, app_page_url: str) -> None:
        """Log the app page URL instead of displaying it."""
        logger.debug("Modal app page: {}", app_page_url)
        self._app_page_url = app_page_url

    def update_task_state(self, task_id: str, state: int) -> None:
        """Suppress task state updates."""
        pass


@contextlib.contextmanager
def enable_modal_output_capture(
    is_logging_to_loguru: bool = True,
) -> Generator[tuple[StringIO, ModalLoguruWriter | None], None, None]:
    """Context manager for capturing Modal app output.

    Intercepts Modal's output system and routes it to a StringIO buffer for
    programmatic inspection. The buffer can be used to detect build failures
    by inspecting the captured output after operations complete.

    When is_logging_to_loguru is True (default), Modal output is also logged
    to loguru with deduplication to avoid spam from repeated status messages.

    Yields a tuple of (output_buffer, loguru_writer) where loguru_writer contains
    app_id and app_name fields that can be set for structured logging, or is
    None if is_logging_to_loguru is False.
    """
    output_buffer = StringIO()
    loguru_writer: ModalLoguruWriter | None = _create_modal_loguru_writer() if is_logging_to_loguru else None

    # Build list of writers to tee output to
    writers: list[Any] = [output_buffer]
    if loguru_writer is not None:
        writers.append(loguru_writer)

    multi_writer = _create_multi_writer(writers)

    with modal.enable_output(show_progress=True):
        with log_span("enabling Modal output capture"):
            output_manager = _QuietOutputManager()
            # Set _stdout to capture Modal's output (build logs, status messages, etc.)
            # This only captures what Modal writes to its OutputManager, not all stdout/stderr
            output_manager._stdout = multi_writer
            output_manager._timestamps = set()
            OutputManager._instance = output_manager

        yield output_buffer, loguru_writer
