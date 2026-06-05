"""Live verification of assorted documented ``query()`` behaviors.

General-purpose checks of the request/response contract: prompt handling, format steering,
session isolation between independent queries, and the textual result fields.
"""

from pathlib import Path

import pytest

from imbue.mngr_robinhood.testing import collect_assistant_text
from imbue.mngr_robinhood.testing import collect_query_messages
from imbue.mngr_robinhood.testing import find_result_message
from imbue.mngr_robinhood.testing import make_sdk_options

pytestmark = [pytest.mark.sdk_live, pytest.mark.asyncio, pytest.mark.timeout(600)]


async def test_query_handles_unicode_prompt(sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(
        "Reply with exactly the word CAFE after reading this: café résumé 日本語 🎉",
        make_sdk_options(sdk_live_model, sdk_cwd),
    )
    assert find_result_message(messages).is_error is False
    assert "CAFE" in collect_assistant_text(messages).upper()


async def test_query_handles_multiline_prompt(sdk_live_model: str, sdk_cwd: Path) -> None:
    prompt = "Here are instructions:\nLine one.\nLine two.\nNow reply with exactly the word MULTILINEOK."
    messages = await collect_query_messages(prompt, make_sdk_options(sdk_live_model, sdk_cwd))
    assert "MULTILINEOK" in collect_assistant_text(messages).upper()


async def test_query_can_do_arithmetic(sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(
        "What is 17 multiplied by 3? Reply with just the number.", make_sdk_options(sdk_live_model, sdk_cwd)
    )
    assert "51" in collect_assistant_text(messages)


async def test_query_follows_simple_format_instruction(sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(
        "Respond with exactly the word LOUDTOKEN in all capital letters and nothing else.",
        make_sdk_options(sdk_live_model, sdk_cwd),
    )
    assert "LOUDTOKEN" in collect_assistant_text(messages)


async def test_query_can_produce_a_list(sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(
        "List the integers from 1 to 12 separated by spaces.", make_sdk_options(sdk_live_model, sdk_cwd)
    )
    text = collect_assistant_text(messages)
    assert "1" in text and "12" in text


async def test_independent_queries_do_not_share_memory(sdk_live_model: str, sdk_cwd: Path) -> None:
    # Two separate query() calls (no resume/continue) get distinct sessions, so the second
    # must not know a secret introduced only in the first.
    await collect_query_messages(
        "Remember that the secret animal is AARDVARK99. Reply OK.", make_sdk_options(sdk_live_model, sdk_cwd)
    )
    second = await collect_query_messages(
        "What is the secret animal I told you earlier? If you were not told, say UNKNOWNANIMAL.",
        make_sdk_options(sdk_live_model, sdk_cwd),
    )
    assert "AARDVARK99" not in collect_assistant_text(second).upper()


async def test_query_result_field_is_nonempty_text(sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages("Reply with a short greeting.", make_sdk_options(sdk_live_model, sdk_cwd))
    result = find_result_message(messages)
    assert isinstance(result.result, str)
    assert result.result.strip() != ""


async def test_query_assistant_text_is_nonempty(sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(
        "Tell me a one-sentence fun fact.", make_sdk_options(sdk_live_model, sdk_cwd)
    )
    assert collect_assistant_text(messages).strip() != ""


async def test_query_answers_factual_question_without_tools(sdk_live_model: str, sdk_cwd: Path) -> None:
    messages = await collect_query_messages(
        "What is the capital of Japan? Answer in one word, no tools.", make_sdk_options(sdk_live_model, sdk_cwd)
    )
    assert "TOKYO" in collect_assistant_text(messages).upper()


async def test_query_system_prompt_preset_without_append_runs(sdk_live_model: str, sdk_cwd: Path) -> None:
    # The bare claude_code preset (no append) should still produce a successful turn.
    options = make_sdk_options(sdk_live_model, sdk_cwd, system_prompt={"type": "preset", "preset": "claude_code"})
    messages = await collect_query_messages("Reply with exactly the word PRESETOK.", options)
    assert find_result_message(messages).is_error is False
    assert "PRESETOK" in collect_assistant_text(messages).upper()
