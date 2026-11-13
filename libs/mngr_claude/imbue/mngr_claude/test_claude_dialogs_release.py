from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.dialogs import BENIGN_FOOTER_REGION_LINES
from imbue.mngr_claude.dialogs import GenericBenign
from imbue.mngr_claude.dialogs import ModelSwitchWarning
from imbue.mngr_claude.dialogs import SELECTOR_HIGHLIGHTED_OPTION_RE
from imbue.mngr_claude.dialogs import get_input_row
from imbue.mngr_claude.dialogs import is_option_highlighted
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import capture_agent_pane_without_input_row_hints
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import records_after_user_prompt
from imbue.mngr_claude.testing import send_keys_to_agent_pane
from imbue.mngr_claude.testing import session_message_text
from imbue.mngr_claude.testing import setup_ctx_that_answers_dialogs
from imbue.mngr_claude.testing import wait_for_agent_pane
from imbue.mngr_claude.testing import wait_for_agent_prompt
from imbue.mngr_claude.testing import wait_for_native_session_file
from imbue.mngr_claude.testing import wait_for_turn_to_end

_SWITCH_TARGET_MODEL: Final[str] = "sonnet"
_SWITCH_COMMAND: Final[str] = f"/model {_SWITCH_TARGET_MODEL}"
_MODEL_PICKER_COMMAND: Final[str] = "/model"
_FIRST_PROMPT: Final[str] = "Reply with just OK."
# Digit-free: a digit typed into a switch warning answers it.
_SWITCH_FOLLOW_UP: Final[str] = "Reply with just the word SWITCHED."
_FOLLOW_UP_SEND_TIMEOUT_SECONDS: Final[float] = 180.0
_PANE_TIMEOUT_SECONDS: Final[float] = 30.0
_SWITCH_CONSUMER: Final[str] = (
    "dialogs.ModelSwitchWarning, read by classify() and deal_with_dialogs() in ClaudeAgent._preflight_send_message"
)
_SYNTHETIC_MODEL: Final[str] = "<synthetic>"


def _wait_for_pane(ctx: AgentReleaseContext, agent_name: str, is_shown: Callable[[str], bool], *, failure: str) -> str:
    return wait_for_agent_pane(
        ctx,
        agent_name,
        is_shown,
        capture=capture_agent_pane_without_input_row_hints,
        timeout=_PANE_TIMEOUT_SECONDS,
        failure=failure,
    )


def _first_reply_after(session_file: Path, prompt_text: str) -> Mapping[str, Any] | None:
    """The first assistant record after the user record carrying ``prompt_text``, or None."""
    for record in records_after_user_prompt(read_session_records(session_file), prompt_text):
        if record.get("type") == "assistant" and isinstance((record.get("message") or {}).get("model"), str):
            return record
    return None


def _wait_for_reply_model(session_file: Path, prompt_text: str, drift: str) -> str:
    reply, _, _ = poll_for_value(
        lambda: _first_reply_after(session_file, prompt_text), timeout=TURN_PROGRESS_TIMEOUT_SECONDS, poll_interval=2.0
    )
    assert reply is not None, f"no assistant record answered {prompt_text!r} {drift}"
    model = reply["message"]["model"]
    assert model != _SYNTHETIC_MODEL, (
        f"claude answered {prompt_text!r} with its own notice instead of a model reply: "
        f"{session_message_text(reply)!r} {drift}"
    )
    return model


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_model_switch_warning_is_confirmed_when_the_agent_answers_dialogs(tmp_path: Path) -> None:
    """With ALL_KNOWN_DIALOGS, the next send confirms a pending ``/model`` switch, so its reply comes from the new model.

    Pins the model-switch confirmation's caption, its "Yes, switch to" option and the indented
    ``❯ N.`` selector row that mngr walks with Down to reach that option.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = setup_ctx_that_answers_dialogs(profile, tmp_path)
    agent_name = f"claude-modelswitch-{get_short_random_string()}"
    drift = drift_note(ctx, "the model-switch confirmation")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, _FIRST_PROMPT)
        session_file = wait_for_native_session_file(ctx)
        first_model = _wait_for_reply_model(session_file, _FIRST_PROMPT, drift)
        wait_for_turn_to_end(profile, ctx, _FIRST_PROMPT, drift)

        send_expecting_success(profile, ctx, agent_name, _SWITCH_COMMAND)
        warning = ModelSwitchWarning()
        pane = _wait_for_pane(
            ctx,
            agent_name,
            warning.matches,
            failure=f"after a completed turn {_SWITCH_COMMAND!r} showed no confirmation matching "
            f"{warning.get_pattern().pattern!r} {drift}; either claude reworded the caption or it switched without "
            f"asking; consumer: {_SWITCH_CONSUMER}",
        )
        option_label = warning.get_option_label()
        assert option_label in pane and SELECTOR_HIGHLIGHTED_OPTION_RE.search(pane) is not None, (
            f"the model-switch confirmation shows no {option_label!r} option or no highlighted numbered option "
            f"{drift}; consumers: ModelSwitchWarning.get_option_label and SELECTOR_HIGHLIGHTED_OPTION_RE, read by "
            f"cycle_to_option\n{pane}"
        )

        def is_highlight_off_the_option(shown: str) -> bool:
            return (
                warning.matches(shown)
                and SELECTOR_HIGHLIGHTED_OPTION_RE.search(shown) is not None
                and not is_option_highlighted(shown, option_label)
            )

        if is_option_highlighted(pane, option_label):
            send_keys_to_agent_pane(ctx, agent_name, "Down", is_literal=False)
        _wait_for_pane(
            ctx,
            agent_name,
            is_highlight_off_the_option,
            failure=f"Down did not move the confirmation's highlight off {option_label!r} to another numbered option "
            f"(if it stayed on the option, the selector may no longer wrap) {drift}; consumer: dialogs.cycle_to_option",
        )

        send = profile.run_mngr(
            ctx, "message", agent_name, "--message", _SWITCH_FOLLOW_UP, timeout=_FOLLOW_UP_SEND_TIMEOUT_SECONDS
        )
        assert send.returncode == 0, (
            f"with ALL_KNOWN_DIALOGS the send after {_SWITCH_COMMAND!r} failed instead of confirming the switch "
            f"{drift}; consumer: {_SWITCH_CONSUMER}, then Answerable.deal_with and cycle_to_option:\n"
            f"{send.stdout}\n{send.stderr}"
        )
        reply_model = _wait_for_reply_model(session_file, _SWITCH_FOLLOW_UP, drift)
        assert _SWITCH_TARGET_MODEL in reply_model, (
            f"the reply after the confirmed switch came from {reply_model!r} (first turn: {first_model!r}), "
            f"not a {_SWITCH_TARGET_MODEL} model {drift}; consumer: {_SWITCH_CONSUMER}"
        )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_open_model_picker_is_escaped_by_the_next_send(tmp_path: Path) -> None:
    """Without opt-in, a ``/model`` picker left open is escaped by the next send, which then reaches claude.

    Pins the picker's "Esc to cancel" footer in the region GenericBenign reads.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-pickerescape-{get_short_random_string()}"
    drift = drift_note(ctx, "the /model picker")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        wait_for_agent_prompt(
            ctx,
            agent_name,
            failure=f"the agent never drew claude's input prompt {drift}; consumer: dialogs.has_input_prompt_line",
        )
        send_keys_to_agent_pane(ctx, agent_name, _MODEL_PICKER_COMMAND, is_literal=True)
        _wait_for_pane(
            ctx,
            agent_name,
            lambda pane: _MODEL_PICKER_COMMAND in (get_input_row(pane) or ""),
            failure=f"the typed /model never reached claude's input row {drift}; consumer: dialogs.get_input_row",
        )
        send_keys_to_agent_pane(ctx, agent_name, "Enter", is_literal=False)
        benign = GenericBenign()
        _wait_for_pane(
            ctx,
            agent_name,
            benign.matches,
            failure=f"the /model picker has no {benign.get_pattern().pattern!r} footer in its last "
            f"{BENIGN_FOOTER_REGION_LINES} lines {drift}; consumer: dialogs.GenericBenign, read by classify() in "
            "ClaudeAgent._preflight_send_message",
        )
        escaping_send = profile.run_mngr(
            ctx, "message", agent_name, "--message", _FIRST_PROMPT, timeout=_FOLLOW_UP_SEND_TIMEOUT_SECONDS
        )
        assert escaping_send.returncode == 0, (
            f"a send with the /model picker open failed instead of escaping it {drift}; consumer: "
            f"dialogs.GenericBenign via deal_with_dialogs:\n{escaping_send.stdout}\n{escaping_send.stderr}"
        )
        wait_for_turn_to_end(profile, ctx, _FIRST_PROMPT, drift)
