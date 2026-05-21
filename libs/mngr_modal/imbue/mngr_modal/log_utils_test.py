import asyncio
from io import StringIO

from loguru import logger
from modal._output.manager import OutputManager
from modal_proto import api_pb2

from imbue.modal_proxy.log_utils import ModalLoguruWriter
from imbue.modal_proxy.log_utils import _create_modal_loguru_writer
from imbue.modal_proxy.log_utils import _create_multi_writer
from imbue.modal_proxy.log_utils import enable_modal_output_capture


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


def test_enable_modal_output_capture_routes_streaming_build_logs_to_buffer_and_loguru() -> None:
    """Build-time log lines from Modal's streaming path land in both the StringIO + loguru BUILD level.

    When Modal builds an image, each line of build output flows through the
    SDK as ``OutputManager.get().put_streaming_log(<TaskLogs>)`` (see
    ``modal.image._image_await_build_result``). The active manager --
    installed by :func:`enable_modal_output_capture` -- must tee that
    payload into the captured StringIO buffer AND, when loguru
    forwarding is enabled, emit a BUILD-level loguru record per line so
    the build output ends up in mngr's normal log stream.

    Regression test: Modal 1.4.x reshaped the OutputManager class
    hierarchy + the internal log path (``_console_print_log`` /
    ``_print_log_buffered``); pre-1.4.x this test exercised
    ``put_log_content`` writing through ``_stdout``. If a future
    upgrade reroutes Modal's build-log writes through a different
    method this test should fail loudly.
    """
    build_lines = [
        "Step 1/3 : FROM python:3.12-slim\n",
        " ---> using cached layer\n",
        "Step 2/3 : RUN pip install --no-cache-dir loguru\n",
    ]

    loguru_records: list[str] = []
    sink_id = logger.add(
        lambda msg: loguru_records.append(msg.record["message"]),
        level="BUILD",
        format="{message}",
        filter=lambda record: record["level"].name == "BUILD",
    )
    try:
        with enable_modal_output_capture(is_logging_to_loguru=True) as (buffer, writer):
            assert writer is not None
            for line in build_lines:
                task_log = api_pb2.TaskLogs(data=line, file_descriptor=1)
                asyncio.run(OutputManager.get().put_streaming_log(task_log))
            OutputManager.get().flush_lines()
            captured = buffer.getvalue()
    finally:
        logger.remove(sink_id)

    # StringIO buffer captured every line verbatim (the order matters so
    # downstream readers can re-stream build output in the right
    # sequence; the assertion intentionally uses a join rather than an
    # ``in`` so a misordered or duplicated line surfaces).
    assert captured == "".join(build_lines), captured

    # Each completed (newline-terminated) line emitted exactly one
    # BUILD-level loguru record; the writer strips trailing whitespace
    # before logging.
    expected_loguru_payloads = [line.strip() for line in build_lines]
    assert loguru_records == expected_loguru_payloads, loguru_records


def test_enable_modal_output_capture_routes_fetched_build_logs_to_buffer() -> None:
    """The ``modal logs`` historical-fetch path (``put_fetched_log``) also tees into the capture.

    Parallels :func:`test_enable_modal_output_capture_routes_streaming_build_logs_to_buffer_and_loguru`
    for the bulk-fetch code path Modal uses when replaying past build
    logs. Same regression guarantee.
    """
    chunks = ["partial line without newline ", "second half\nline two\n"]

    with enable_modal_output_capture(is_logging_to_loguru=False) as (buffer, _):
        for chunk in chunks:
            task_log = api_pb2.TaskLogs(data=chunk, file_descriptor=2)
            asyncio.run(OutputManager.get().put_fetched_log(task_log))
        OutputManager.get().flush_lines()
        captured = buffer.getvalue()

    assert captured == "".join(chunks), captured
