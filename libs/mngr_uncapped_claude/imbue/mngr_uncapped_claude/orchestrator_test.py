import io
import time

from imbue.mngr_uncapped_claude.data_types import OutputFormat
from imbue.mngr_uncapped_claude.orchestrator import _build_agent_name
from imbue.mngr_uncapped_claude.orchestrator import _build_pass_env_vars
from imbue.mngr_uncapped_claude.orchestrator import _build_result_meta
from imbue.mngr_uncapped_claude.orchestrator import monotonic_ms_since
from imbue.mngr_uncapped_claude.output_modes import StreamingOutputWriter


def test_build_agent_name_has_uncapped_prefix() -> None:
    name = _build_agent_name()
    assert str(name).startswith("uncapped-")
    assert len(str(name)) > len("uncapped-")


def test_build_pass_env_vars_is_populated() -> None:
    options = _build_pass_env_vars()
    assert len(options.env_vars) > 0


def test_build_result_meta_records_error_text() -> None:
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="s", stdout=io.StringIO())
    meta = _build_result_meta(writer=writer, start_time=0.0, agent_id="agent-x", error_text="boom")
    assert meta.is_error
    assert meta.error_text == "boom"
    assert meta.session_id == "agent-x"


def test_build_result_meta_no_error() -> None:
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="s", stdout=io.StringIO())
    meta = _build_result_meta(writer=writer, start_time=0.0, agent_id="agent-x", error_text=None)
    assert not meta.is_error
    assert meta.error_text is None


def test_monotonic_ms_since_returns_non_negative_int() -> None:
    start = time.monotonic()
    elapsed = monotonic_ms_since(start)
    assert isinstance(elapsed, int)
    assert elapsed >= 0
