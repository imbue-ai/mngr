import shlex
from pathlib import Path

import pytest

from imbue.mngr.utils.ssh import build_ssh_connect_command
from imbue.mngr.utils.ssh import quote_ssh_option_value
from imbue.mngr.utils.ssh import quote_ssh_option_value_for_shell


def test_build_ssh_connect_command_includes_pin_options_when_known_hosts_is_known() -> None:
    command = build_ssh_connect_command("root", "203.0.113.5", 2222, Path("/keys/id"), Path("/keys/known_hosts"))
    assert command == (
        "ssh -i /keys/id -o UserKnownHostsFile='\"/keys/known_hosts\"' -o StrictHostKeyChecking=yes "
        "-p 2222 root@203.0.113.5"
    )


def test_build_ssh_connect_command_omits_pin_options_when_no_known_hosts() -> None:
    command = build_ssh_connect_command("root", "203.0.113.5", 2222, Path("/keys/id"), None)
    assert command == "ssh -i /keys/id -p 2222 root@203.0.113.5"


def test_build_ssh_connect_command_survives_paths_with_spaces() -> None:
    """The command is pasted into a shell, so it must survive both the shell's split and ssh's.

    A home directory such as `/Users/Jane Doe` puts a space in every profile-scoped path.
    """
    command = build_ssh_connect_command(
        "root",
        "203.0.113.5",
        2222,
        Path("/keys dir/id"),
        Path("/keys dir/known_hosts"),
    )

    parsed = shlex.split(command)
    assert "/keys dir/id" in parsed
    option = next(arg for arg in parsed if arg.startswith("UserKnownHostsFile="))
    assert option == 'UserKnownHostsFile="/keys dir/known_hosts"', option


def test_ssh_read_value_carries_its_own_quotes_and_no_shell_quoting() -> None:
    """A value handed straight to exec needs the inner quotes and nothing else.

    Adding shell quoting here would put literal single quotes into the filename
    ssh opens, so the two helpers are not interchangeable.
    """
    spaced = Path("/keys dir/known_hosts")
    assert quote_ssh_option_value(spaced) == '"/keys dir/known_hosts"'


def test_shell_quoting_reduces_to_the_ssh_read_form_once_a_shell_has_parsed_it() -> None:
    """The extra layer exists only to survive the shell, and leaves the same value behind."""
    spaced = Path("/keys dir/known_hosts")
    option = f"UserKnownHostsFile={quote_ssh_option_value_for_shell(spaced)}"
    after_shell = shlex.split(option)
    assert len(after_shell) == 1, after_shell
    assert after_shell[0] == f"UserKnownHostsFile={quote_ssh_option_value(spaced)}"


@pytest.mark.parametrize("path", ("/keys/known_hosts", "/keys dir/known_hosts"))
def test_shell_form_survives_the_shell_and_leaves_ssh_one_file(path: str) -> None:
    """Both quoting layers are load-bearing: the shell must yield one word, ssh one filename.

    ssh reads UserKnownHostsFile as a whitespace-separated list, so it splits the value a
    second time after the shell is done with it.
    """
    option = f"UserKnownHostsFile={quote_ssh_option_value_for_shell(path)}"

    (after_shell,) = shlex.split(option)
    assert after_shell == f'UserKnownHostsFile="{path}"'

    value = after_shell.removeprefix("UserKnownHostsFile=")
    assert shlex.split(value) == [path]
