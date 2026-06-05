import pytest

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


@pytest.mark.parametrize("flag", sorted(REJECTED_FLAGS.keys()))
def test_rejected_flags_raise(flag: str) -> None:
    with pytest.raises(UnsupportedClaudeFlagError):
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
