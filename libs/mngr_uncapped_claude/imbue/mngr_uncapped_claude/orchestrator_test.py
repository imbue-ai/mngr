import time

from imbue.mngr_uncapped_claude.orchestrator import _TranscriptReadFailureWarner
from imbue.mngr_uncapped_claude.orchestrator import _build_agent_name
from imbue.mngr_uncapped_claude.orchestrator import _build_pass_env_vars
from imbue.mngr_uncapped_claude.orchestrator import _build_result_meta
from imbue.mngr_uncapped_claude.orchestrator import monotonic_ms_since


def test_build_agent_name_has_uncapped_prefix() -> None:
    name = _build_agent_name()
    assert str(name).startswith("uncapped-")
    assert len(str(name)) > len("uncapped-")


def test_build_pass_env_vars_is_populated() -> None:
    options = _build_pass_env_vars()
    assert len(options.env_vars) > 0


def test_build_result_meta_records_error_text() -> None:
    meta = _build_result_meta(start_time=0.0, agent_id="agent-x", error_text="boom")
    assert meta.is_error
    assert meta.error_text == "boom"
    assert meta.session_id == "agent-x"


def test_build_result_meta_no_error() -> None:
    meta = _build_result_meta(start_time=0.0, agent_id="agent-x", error_text=None)
    assert not meta.is_error
    assert meta.error_text is None


def test_monotonic_ms_since_returns_non_negative_int() -> None:
    start = time.monotonic()
    elapsed = monotonic_ms_since(start)
    assert isinstance(elapsed, int)
    assert elapsed >= 0


def test_transcript_read_failure_warner_warns_once() -> None:
    warner = _TranscriptReadFailureWarner()
    assert not warner.has_warned
    warner.warn(RuntimeError("first failure"))
    assert warner.has_warned
    # Subsequent calls must not flip the flag back off or otherwise raise.
    warner.warn(RuntimeError("second failure"))
    assert warner.has_warned
