import contextlib
from io import StringIO
from typing import Any
from typing import Generator
from typing import Sequence

import modal
from loguru import logger
from modal._output.manager import OutputManager
from modal._output.rich import RichOutputManager

from imbue.imbue_common.logging import log_span
from imbue.mngr.primitives import LogLevel
from imbue.mngr.utils.logging import register_build_level

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


class _QuietOutputManager(RichOutputManager):
    """Modal OutputManager that suppresses interactive output and tees log content to a writer.

    Modal's default ``RichOutputManager`` displays spinners, progress bars,
    object trees, and app-page-URL banners which don't work well when
    capturing output programmatically. This subclass enables Modal's
    built-in quiet mode (which already no-ops spinners + progress UI),
    routes the per-task log content to a configurable writer (we tee it
    to a StringIO and the loguru writer below), and downgrades the
    app-page-URL print to a debug log so the captured StringIO doesn't
    contain that banner.

    The shape of Modal's OutputManager API changed substantially between
    1.3.x (concrete class + ``_stdout`` slot + ``put_log_content``) and
    1.4.x (ABC + ``put_streaming_log`` / ``put_fetched_log`` that go
    through a Rich Console). This class implements the 1.4.x surface;
    use ``enable_modal_output_capture`` below to install it.
    """

    _captured_writer: Any

    def update_app_page_url(self, app_page_url: str) -> None:
        """Log the app page URL instead of displaying it."""
        logger.debug("Modal app page: {}", app_page_url)
        self._app_page_url = app_page_url

    def _console_print_log(self, fd: int, data: str) -> None:
        """Tee log content into the captured writer instead of stdout."""
        self._captured_writer.write(data)

    def _print_log_buffered(self, fd: int, data: str) -> None:
        """Tee log content into the captured writer instead of stdout."""
        self._captured_writer.write(data)


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

    # ``modal.enable_output`` (1.4.x) installs a fresh RichOutputManager
    # via ``OutputManager._set``. We let it install one, then swap our
    # quiet capture variant in for the duration of the block. The outer
    # ``enable_output`` is still required so internal modal code paths
    # that gate on ``OutputManager.get().is_enabled`` see "yes".
    with modal.enable_output():
        with log_span("enabling Modal output capture"):
            previous_manager = OutputManager.get()
            capture_manager = _QuietOutputManager()
            capture_manager.set_quiet_mode(True)
            capture_manager._captured_writer = multi_writer
            OutputManager._set(capture_manager)
        try:
            yield output_buffer, loguru_writer
        finally:
            OutputManager._set(previous_manager)
