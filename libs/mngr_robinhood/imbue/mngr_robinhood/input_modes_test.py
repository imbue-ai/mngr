import io
from collections.abc import Iterator

import pytest

from imbue.mngr_robinhood.data_types import InputFormat
from imbue.mngr_robinhood.errors import InvalidStreamJsonInputError
from imbue.mngr_robinhood.errors import MissingPromptError
from imbue.mngr_robinhood.input_modes import iter_user_prompts


def _collect(it: Iterator[str]) -> list[str]:
    return list(it)


def test_text_positional_wins() -> None:
    stdin = io.StringIO("stdin content")
    prompts = _collect(iter_user_prompts(InputFormat.TEXT, "the-prompt", stdin, is_stdin_a_tty=False))
    assert prompts == ["the-prompt"]


def test_text_stdin_used_when_no_positional_and_not_tty() -> None:
    stdin = io.StringIO("piped content")
    prompts = _collect(iter_user_prompts(InputFormat.TEXT, None, stdin, is_stdin_a_tty=False))
    assert prompts == ["piped content"]


def test_text_raises_when_no_prompt_and_tty() -> None:
    stdin = io.StringIO("")
    with pytest.raises(MissingPromptError):
        _collect(iter_user_prompts(InputFormat.TEXT, None, stdin, is_stdin_a_tty=True))


def test_text_raises_when_no_prompt_and_empty_stdin() -> None:
    stdin = io.StringIO("")
    with pytest.raises(MissingPromptError):
        _collect(iter_user_prompts(InputFormat.TEXT, None, stdin, is_stdin_a_tty=False))


def test_stream_json_single_line() -> None:
    stdin = io.StringIO('{"type":"user","message":{"role":"user","content":"hello"}}\n')
    prompts = _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))
    assert prompts == ["hello"]


def test_stream_json_multiple_lines() -> None:
    stdin = io.StringIO(
        '{"type":"user","message":{"role":"user","content":"first"}}\n'
        '{"type":"user","message":{"role":"user","content":"second"}}\n'
    )
    prompts = _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))
    assert prompts == ["first", "second"]


def test_stream_json_empty_lines_are_skipped() -> None:
    stdin = io.StringIO(
        '\n{"type":"user","message":{"role":"user","content":"a"}}\n\n'
        '{"type":"user","message":{"role":"user","content":"b"}}\n\n'
    )
    prompts = _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))
    assert prompts == ["a", "b"]


def test_stream_json_malformed_line() -> None:
    stdin = io.StringIO("not-json\n")
    with pytest.raises(InvalidStreamJsonInputError, match="not valid JSON"):
        _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))


def test_stream_json_wrong_type() -> None:
    stdin = io.StringIO('{"type":"control_request","request":{}}\n')
    with pytest.raises(InvalidStreamJsonInputError, match="only.*user.*supported"):
        _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))


def test_stream_json_non_string_content() -> None:
    stdin = io.StringIO('{"type":"user","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n')
    with pytest.raises(InvalidStreamJsonInputError, match="content must be a string"):
        _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))


def test_stream_json_missing_message() -> None:
    stdin = io.StringIO('{"type":"user"}\n')
    with pytest.raises(InvalidStreamJsonInputError, match="message"):
        _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))


def test_stream_json_wrong_role() -> None:
    stdin = io.StringIO('{"type":"user","message":{"role":"assistant","content":"hi"}}\n')
    with pytest.raises(InvalidStreamJsonInputError, match="role"):
        _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))


def test_stream_json_top_level_array_rejected() -> None:
    stdin = io.StringIO("[]\n")
    with pytest.raises(InvalidStreamJsonInputError, match="JSON object"):
        _collect(iter_user_prompts(InputFormat.STREAM_JSON, None, stdin, is_stdin_a_tty=False))


def test_stream_json_positional_prompt_rejected() -> None:
    stdin = io.StringIO('{"type":"user","message":{"role":"user","content":"hi"}}\n')
    with pytest.raises(InvalidStreamJsonInputError, match="positional prompt"):
        _collect(iter_user_prompts(InputFormat.STREAM_JSON, "stray-positional", stdin, is_stdin_a_tty=False))


def test_stream_json_empty_positional_allowed() -> None:
    # The empty-string positional is the canonical "no positional" representation
    # used elsewhere; it should be treated the same as None and not trigger the
    # incompatible-combination guard.
    stdin = io.StringIO('{"type":"user","message":{"role":"user","content":"hi"}}\n')
    prompts = _collect(iter_user_prompts(InputFormat.STREAM_JSON, "", stdin, is_stdin_a_tty=False))
    assert prompts == ["hi"]
