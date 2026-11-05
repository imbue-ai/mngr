from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import content_text
from imbue.mngr_claude.testing import record_one_proxied_turn

_ROLE_TAGS: Final[tuple[str, ...]] = ("ROLE-ALPHA-7Q3", "ROLE-BRAVO-9K2")
_ROLE_BLOCKS: Final[tuple[str, ...]] = tuple(
    f"Every reply you write must contain the tag {tag}." for tag in _ROLE_TAGS
)
_APPEND_CONSUMER: Final[str] = (
    "ClaudeAgent._build_append_system_prompt_args, which joins every append_system_prompt block into one "
    "--append-system-prompt flag"
)


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_agent_sends_every_stacked_system_prompt_block(tmp_path: Path) -> None:
    """An agent whose config stacks two append_system_prompt blocks sends both in one system prompt.

    Pins that the text of claude's last ``--append-system-prompt`` flag reaches the system prompt
    it sends; the flag is last-wins.
    """
    reason = ClaudeReleaseProfile().unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    bodies, drift = record_one_proxied_turn(
        tmp_path,
        f"\n[agent_types.claude]\nappend_system_prompt = {json.dumps(list(_ROLE_BLOCKS))}\n",
        "--append-system-prompt",
    )
    system_prompts = [content_text(body.get("system")) for body in bodies]
    assert any(all(tag in prompt for tag in _ROLE_TAGS) for prompt in system_prompts), (
        f"none of the {len(system_prompts)} /v1/messages system prompts the agent sent carries every role "
        f"tag {_ROLE_TAGS} {drift}; tags each carries: "
        f"{[[tag for tag in _ROLE_TAGS if tag in prompt] for prompt in system_prompts]}; "
        f"consumer: {_APPEND_CONSUMER}"
    )
