"""Rendering ssh invocations as text: option values, and the commands holding them.

ssh tokenizes an option value on whitespace, so a path reaches it intact only if
it carries quotes -- and a value a shell reads first needs a second layer.

Separate from ``providers.ssh_utils``, which handles the key material and
known_hosts *files* themselves: this module only builds strings, holds no state,
and sits low enough in the layer order for every caller to reach it.
"""

import shlex
from pathlib import Path


def quote_ssh_option_value(value: Path | str) -> str:
    """Quote an ssh option value that ssh itself parses.

    ssh documents options like ``UserKnownHostsFile`` as whitespace-separated
    lists and splits the value itself, and it tokenizes a config-file line the
    same way, so a value containing a space needs double quotes to stay one
    item. Applies to any such option -- ``IdentityFile``, ``ControlPath``,
    ``CertificateFile`` -- not only to a known_hosts path.

    For a value ssh reads directly: one element of an exec'd argv, or one line
    of a generated ssh config. When a shell reads the command first, use
    :func:`quote_ssh_option_value_for_shell`.
    """
    return f'"{value}"'


def quote_ssh_option_value_for_shell(value: Path | str) -> str:
    """Quote an ssh option value that a shell parses before ssh does.

    Two layers, both load-bearing: the inner double quotes keep ssh from
    splitting the value, and the outer shell quoting keeps the whole thing a
    single word. The doubled wrapping reads as redundant and is not -- dropping
    either one lets a spaced path through as several.

    For values reaching ssh through a shell: ``GIT_SSH_COMMAND``, rsync ``-e``,
    a wrapper script, a command a user pastes. Applying this to a value ssh
    reads directly would put literal quotes into the filename it opens.
    """
    return shlex.quote(quote_ssh_option_value(value))


def build_ssh_connect_command(user: str, host: str, port: int, key_path: Path, known_hosts_path: Path | None) -> str:
    """Build the human-facing ssh command for an SSHInfo, verifying pins when a known_hosts file is known.

    A shell parses whatever the reader pastes, so every path here is quoted for
    it, and the known_hosts value carries ssh's own quotes underneath.
    """
    if known_hosts_path is not None:
        quoted_known_hosts = quote_ssh_option_value_for_shell(known_hosts_path)
        pin_options = f" -o UserKnownHostsFile={quoted_known_hosts} -o StrictHostKeyChecking=yes"
    else:
        pin_options = ""
    return f"ssh -i {shlex.quote(str(key_path))}{pin_options} -p {port} {user}@{host}"
