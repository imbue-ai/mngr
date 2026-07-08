import re

import pytest

from imbue.mngr.primitives import TmuxWindowSize
from imbue.mngr_robinhood.arg_partition import REJECTED_FLAGS
from imbue.mngr_robinhood.arg_partition import partition_args
from imbue.mngr_robinhood.data_types import InputFormat
from imbue.mngr_robinhood.data_types import OutputFormat
from imbue.mngr_robinhood.errors import UnsupportedClaudeFlagError


def test_empty_argv() -> None:
    partition = partition_args(())
    assert partition.input_format == InputFormat.TEXT
    assert partition.output_format == OutputFormat.TEXT
    assert not partition.replay_user_messages
    assert partition.positional_prompt is None
    assert partition.pass_through_agent_args == ()


def test_positional_prompt_is_captured() -> None:
    partition = partition_args(("hello",))
    assert partition.positional_prompt == "hello"
    assert partition.pass_through_agent_args == ()


def test_print_flag_is_consumed_not_forwarded() -> None:
    partition = partition_args(("-p", "hello"))
    assert partition.positional_prompt == "hello"
    assert partition.pass_through_agent_args == ()


def test_print_long_flag_is_consumed() -> None:
    partition = partition_args(("--print", "hello"))
    assert partition.positional_prompt == "hello"
    assert partition.pass_through_agent_args == ()


def test_output_format_value_form() -> None:
    partition = partition_args(("--output-format", "json", "hello"))
    assert partition.output_format == OutputFormat.JSON
    assert partition.positional_prompt == "hello"
    assert partition.pass_through_agent_args == ()


def test_output_format_equals_form() -> None:
    partition = partition_args(("--output-format=stream-json", "--verbose", "hello"))
    assert partition.output_format == OutputFormat.STREAM_JSON
    assert partition.positional_prompt == "hello"
    assert partition.pass_through_agent_args == ("--verbose",)


def test_input_format_value_form() -> None:
    partition = partition_args(("--input-format", "stream-json"))
    assert partition.input_format == InputFormat.STREAM_JSON


def test_input_format_invalid_value() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="--input-format must be"):
        partition_args(("--input-format", "bogus"))


def test_output_format_invalid_value() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="--output-format must be"):
        partition_args(("--output-format=bogus",))


def test_pass_through_args_after_double_dash() -> None:
    partition = partition_args(("--", "--anything-after"))
    assert partition.pass_through_agent_args == ("--anything-after",)


def test_pass_through_claude_flags() -> None:
    partition = partition_args(("--model", "opus", "--allowedTools", "Read,Edit", "hello"))
    assert partition.pass_through_agent_args == ("--model", "opus", "--allowedTools", "Read,Edit")
    assert partition.positional_prompt == "hello"


def test_pass_through_claude_value_flag_equals_form() -> None:
    # The inline ``--flag=value`` form takes a different branch than the
    # space-separated form: the single token is forwarded verbatim and the
    # following token must NOT be consumed as the flag's value, so the prompt
    # placed after it is still recognized as the positional prompt.
    partition = partition_args(("--model=opus", "hello"))
    assert partition.pass_through_agent_args == ("--model=opus",)
    assert partition.positional_prompt == "hello"


@pytest.mark.parametrize("flag", sorted(REJECTED_FLAGS.keys()))
def test_rejected_flags_raise(flag: str) -> None:
    # Assert the raised message is the per-flag reason from REJECTED_FLAGS, not
    # just that *some* UnsupportedClaudeFlagError was raised -- otherwise a bug
    # that paired a flag with the wrong reason (e.g. --resume raising the
    # --continue message) would go uncaught.
    with pytest.raises(UnsupportedClaudeFlagError, match=re.escape(REJECTED_FLAGS[flag])):
        partition_args((flag,))


def test_replay_user_messages_requires_stream_json_both_sides() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="--replay-user-messages"):
        partition_args(("--replay-user-messages",))


def test_replay_user_messages_accepted_with_stream_json() -> None:
    partition = partition_args(
        (
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--replay-user-messages",
        )
    )
    assert partition.replay_user_messages


def test_inline_value_on_bool_flag_rejected() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="does not take a value"):
        partition_args(("--print=foo",))


def test_missing_value_on_value_flag_rejected() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="requires a value"):
        partition_args(("--output-format",))


def test_only_first_positional_is_prompt_others_forwarded() -> None:
    partition = partition_args(("first", "second"))
    assert partition.positional_prompt == "first"
    assert partition.pass_through_agent_args == ("second",)


def test_include_partial_messages_no_longer_rejected() -> None:
    assert "--include-partial-messages" not in REJECTED_FLAGS


def test_include_partial_messages_accepted_with_stream_json() -> None:
    partition = partition_args(("--output-format", "stream-json", "--include-partial-messages", "hi"))
    assert partition.include_partial_messages
    assert partition.positional_prompt == "hi"
    # Consumed by the wrapper, not forwarded to the spawned claude.
    assert partition.pass_through_agent_args == ()


def test_include_partial_messages_requires_stream_json() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="requires --output-format=stream-json"):
        partition_args(("--include-partial-messages", "hi"))


def test_stream_plain_text_accepted_with_text_output() -> None:
    partition = partition_args(("--stream-plain-text", "hi"))
    assert partition.stream_plain_text
    assert partition.positional_prompt == "hi"
    assert partition.pass_through_agent_args == ()


def test_stream_plain_text_rejected_with_json_output() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="requires --output-format=text"):
        partition_args(("--output-format", "json", "--stream-plain-text", "hi"))


def test_streaming_flags_default_false() -> None:
    partition = partition_args(("hi",))
    assert not partition.include_partial_messages
    assert not partition.stream_plain_text


def test_tmux_flags_default_to_wide_pinned_window() -> None:
    partition = partition_args(("hi",))
    assert int(partition.tmux_width) == 2048
    assert int(partition.tmux_height) == 256
    assert partition.tmux_window_size == TmuxWindowSize.MANUAL


def test_tmux_flags_parse_explicit_values() -> None:
    partition = partition_args(("--tmux-width", "120", "--tmux-height", "40", "--tmux-window-size", "latest", "hi"))
    assert int(partition.tmux_width) == 120
    assert int(partition.tmux_height) == 40
    assert partition.tmux_window_size == TmuxWindowSize.LATEST
    assert partition.positional_prompt == "hi"
    assert partition.pass_through_agent_args == ()


def test_tmux_flags_parse_equals_form() -> None:
    partition = partition_args(("--tmux-width=320", "--tmux-window-size=smallest", "hi"))
    assert int(partition.tmux_width) == 320
    assert partition.tmux_window_size == TmuxWindowSize.SMALLEST


def test_tmux_width_rejects_non_positive() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="--tmux-width must be a positive integer"):
        partition_args(("--tmux-width", "0", "hi"))


def test_tmux_height_rejects_non_integer() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="--tmux-height must be a positive integer"):
        partition_args(("--tmux-height", "tall", "hi"))


def test_tmux_window_size_rejects_unknown_value() -> None:
    with pytest.raises(UnsupportedClaudeFlagError, match="--tmux-window-size must be"):
        partition_args(("--tmux-window-size", "huge", "hi"))
