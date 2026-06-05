"""Unit tests for the Telegram bot-token injector command construction.

The full ``inject_telegram_bot_token`` flow spawns a real ``mngr exec``
subprocess, so it is not unit-tested here. The bug-prone part -- assembling the
remote shell snippet and shell-quoting the token -- is factored into
``build_inject_command`` and tested directly.
"""

import shlex

from pydantic import SecretStr

from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.telegram.injector import _SECRETS_FILE
from imbue.minds.telegram.injector import build_inject_command
from imbue.mngr.primitives import AgentId


def test_build_inject_command_targets_mngr_exec_for_the_agent() -> None:
    agent_id = AgentId()
    command = build_inject_command(agent_id, SecretStr("123456:ABCdef"))

    assert command[0] == MNGR_BINARY
    assert command[1] == "exec"
    assert command[2] == str(agent_id)


def test_build_inject_command_writes_token_to_per_secret_env_file() -> None:
    command = build_inject_command(AgentId(), SecretStr("123456:ABCdef"))
    shell_snippet = command[3]

    assert "mkdir -p runtime/secrets" in shell_snippet
    assert f"> {_SECRETS_FILE}" in shell_snippet
    assert "export TELEGRAM_BOT_TOKEN=" in shell_snippet
    # A simple token needs no quoting and appears verbatim.
    assert "123456:ABCdef" in shell_snippet


def test_build_inject_command_shell_quotes_token_with_spaces() -> None:
    token_with_space = "abc def"
    command = build_inject_command(AgentId(), SecretStr(token_with_space))
    shell_snippet = command[3]

    # A token with a space must be wrapped (shlex.quote adds surrounding single
    # quotes) so it stays a single printf argument rather than splitting in two.
    quoted = shlex.quote(token_with_space)
    assert quoted == "'abc def'"
    assert quoted in shell_snippet
    # The token only ever appears inside that quoted region -- never bare.
    assert shell_snippet.count(token_with_space) == 1
    assert f"%s\\n' {quoted} >" in shell_snippet


def test_build_inject_command_neutralizes_shell_metacharacters_in_token() -> None:
    malicious_token = "x$(touch /tmp/pwned);echo"
    command = build_inject_command(AgentId(), SecretStr(malicious_token))
    shell_snippet = command[3]

    quoted = shlex.quote(malicious_token)
    # shlex.quote wraps the whole token in single quotes, so the dangerous
    # substitution/command-separator is inert data, not executable shell.
    assert quoted == "'x$(touch /tmp/pwned);echo'"
    assert quoted in shell_snippet
    # Outside the single-quoted token there is no unescaped command substitution.
    assert "$(touch" not in shell_snippet.replace(quoted, "")
