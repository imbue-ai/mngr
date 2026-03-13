from io import StringIO

from imbue.mng_modal.log_utils import ModalLoguruWriter
from imbue.mng_modal.log_utils import _create_modal_loguru_writer
from imbue.mng_modal.log_utils import _create_multi_writer
from imbue.mng_modal.log_utils import enable_modal_output_capture


def test_multi_writer_writes_to_all_files() -> None:
    """Should write to all file-like objects."""
    buffer1 = StringIO()
    buffer2 = StringIO()
    multi = _create_multi_writer([buffer1, buffer2])

    multi.write("test message")

    assert buffer1.getvalue() == "test message"
    assert buffer2.getvalue() == "test message"


def test_multi_writer_is_not_a_tty() -> None:
    """Should report as not a tty to disable interactive features."""
    multi = _create_multi_writer([])
    assert multi.isatty() is False


def test_multi_writer_context_manager() -> None:
    """Should work as a context manager."""
    buffer = StringIO()
    with _create_multi_writer([buffer]) as multi:
        multi.write("inside context")

    assert buffer.getvalue() == "inside context"


def test_modal_loguru_writer_app_metadata_can_be_set() -> None:
    """Should allow setting app_id and app_name."""
    writer = _create_modal_loguru_writer()
    writer.app_id = "test-app-id"
    writer.app_name = "test-app-name"

    assert writer.app_id == "test-app-id"
    assert writer.app_name == "test-app-name"


def test_modal_loguru_writer_initial_metadata_is_none() -> None:
    """Should have None for app metadata initially."""
    writer = _create_modal_loguru_writer()
    assert writer.app_id is None
    assert writer.app_name is None


def test_modal_loguru_writer_is_writable() -> None:
    """Should report as writable."""
    writer = _create_modal_loguru_writer()
    assert writer.writable() is True
    assert writer.readable() is False
    assert writer.seekable() is False


def test_modal_loguru_writer_deduplicates_messages() -> None:
    """Should deduplicate consecutive messages."""
    writer = _create_modal_loguru_writer()

    writer.write("same message")
    result1 = writer.write("same message")

    assert result1 == len("same message")


def test_enable_modal_output_capture_returns_buffer_and_writer() -> None:
    """Should return a StringIO buffer and optional loguru writer."""
    with enable_modal_output_capture(is_logging_to_loguru=True) as (buffer, writer):
        assert isinstance(buffer, StringIO)
        assert writer is not None
        assert isinstance(writer, ModalLoguruWriter)


def test_enable_modal_output_capture_returns_none_writer_when_disabled() -> None:
    """Should return None for writer when logging to loguru is disabled."""
    with enable_modal_output_capture(is_logging_to_loguru=False) as (buffer, writer):
        assert isinstance(buffer, StringIO)
        assert writer is None
